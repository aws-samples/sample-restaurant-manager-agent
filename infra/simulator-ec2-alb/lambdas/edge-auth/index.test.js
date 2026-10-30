// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use strict";

/**
 * Unit tests for the edge-auth Lambda@Edge handler (spec 029, task 13).
 *
 * Uses the Node built-in test runner (`node --test`) - no extra dependencies.
 * Requires the production deps to be vendored first (`./bundle.sh`), because
 * index.js requires `cognito-at-edge`.
 *
 * Run:  node --test        (or: npm test)
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const DIR = __dirname;
const CONFIG_LOCAL = path.join(DIR, "config.local.json");
const CONFIG_PLACEHOLDER = path.join(DIR, "config.json");

// A valid, fully-populated config like the CDK stack injects at synth (D3-A).
const VALID_CONFIG = {
  region: "us-west-2",
  userPoolId: "us-west-2_TESTPOOL0",
  userPoolAppId: "testappclientid0000000000",
  userPoolDomain: "restaurant-sim-123456789012.auth.us-west-2.amazoncognito.com",
  csrfNonceSecret: "test-only-nonce-secret",
};

test("index.js builds the cognito-at-edge Authenticator from injected config and exports a handler", () => {
  // Inject a valid sidecar so the module-scope Authenticator constructs (the
  // committed config.json placeholder is intentionally empty). Back up and
  // restore any pre-existing sidecar (e.g. left by a CDK synth).
  const backup = fs.existsSync(CONFIG_LOCAL)
    ? fs.readFileSync(CONFIG_LOCAL, "utf8")
    : null;
  fs.writeFileSync(CONFIG_LOCAL, JSON.stringify(VALID_CONFIG, null, 2) + "\n");
  try {
    delete require.cache[require.resolve("./index.js")];
    delete require.cache[require.resolve("./config.local.json")];
    const mod = require("./index.js");
    assert.strictEqual(
      typeof mod.handler,
      "function",
      "index.js must export a handler function",
    );
  } finally {
    delete require.cache[require.resolve("./index.js")];
    if (backup !== null) {
      fs.writeFileSync(CONFIG_LOCAL, backup);
    } else {
      fs.rmSync(CONFIG_LOCAL, { force: true });
    }
  }
});

test("committed config.json is an empty placeholder (no secret in git; ARCC SAX-02)", () => {
  const placeholder = JSON.parse(fs.readFileSync(CONFIG_PLACEHOLDER, "utf8"));
  for (const key of [
    "region",
    "userPoolId",
    "userPoolAppId",
    "userPoolDomain",
    "csrfNonceSecret",
  ]) {
    assert.ok(key in placeholder, `placeholder config.json must define "${key}"`);
    assert.strictEqual(
      placeholder[key],
      "",
      `placeholder config.json "${key}" must be empty - real values are injected at synth`,
    );
  }
});

// ---------------------------------------------------------------------------
// TM-16: manager-identity stamping
// ---------------------------------------------------------------------------

function makeJwt(payload) {
  const b64 = (o) =>
    Buffer.from(JSON.stringify(o)).toString("base64").replace(/=+$/, "");
  return `${b64({ alg: "RS256" })}.${b64(payload)}.signature`;
}

test("extractSub reads sub from a Cognito idToken cookie", () => {
  const { extractSub } = require("./index.js");
  const jwt = makeJwt({ sub: "11111111-2222-3333-4444-555555555555", email: "m@example.com" });
  const request = {
    headers: {
      cookie: [
        {
          key: "Cookie",
          value:
            `CognitoIdentityServiceProvider.abc.mgr.accessToken=xyz; ` +
            `CognitoIdentityServiceProvider.abc.mgr.idToken=${jwt}`,
        },
      ],
    },
  };
  assert.strictEqual(extractSub(request), "11111111-2222-3333-4444-555555555555");
});

test("extractSub returns null when there is no idToken cookie, and never throws", () => {
  const { extractSub } = require("./index.js");
  for (const headers of [
    {},
    { cookie: [] },
    { cookie: [{ key: "Cookie", value: "unrelated=1" }] },
    { cookie: [{ key: "Cookie", value: "x.idToken=not-a-jwt" }] },
    { cookie: [{ key: "Cookie", value: "x.idToken=a.b.c" }] },
    { cookie: [{ key: "Cookie", value: "malformed" }] },
  ]) {
    assert.strictEqual(extractSub({ headers }), null, JSON.stringify(headers));
  }
});

test("extractSub does not forward PII claims -- only sub is returned", () => {
  const { extractSub } = require("./index.js");
  const jwt = makeJwt({ sub: "opaque-id", email: "manager@example.com", "cognito:username": "mgr" });
  const request = { headers: { cookie: [{ key: "Cookie", value: `a.idToken=${jwt}` }] } };
  const out = extractSub(request);
  assert.strictEqual(out, "opaque-id");
  assert.ok(!String(out).includes("@"), "an email must never be the attribution value");
});
