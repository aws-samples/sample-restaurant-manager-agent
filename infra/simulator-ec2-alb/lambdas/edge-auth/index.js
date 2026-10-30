// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

"use strict";

/**
 * Lambda@Edge (viewer-request) Cognito authenticator for the Restaurant
 * Simulator dashboard - spec 029.
 *
 * Enforces Cognito Hosted UI login at the CloudFront edge using the awslabs
 * `cognito-at-edge` library. Unauthenticated viewers are 302-redirected to the
 * Hosted UI; viewers with a valid Cognito session pass through to the origin.
 * This replaces spec 007's ALB `authenticate-cognito` listener action with no
 * application (FastAPI/React) code change.
 *
 * Constraints & decisions (see the CloudFront edge-auth design record):
 *   - Lambda@Edge forbids environment variables, so all configuration is read
 *     from a JSON file beside this handler. The CDK stack writes the real
 *     values into a GITIGNORED sidecar `config.local.json` at synth (decision
 *     D3-A, two-phase bootstrap deploy); this handler prefers that sidecar and
 *     falls back to the committed empty `config.json` placeholder. The real
 *     Cognito IDs / CSRF secret are never committed to git (ARCC SAX-02).
 *   - Decision D2-A: PUBLIC Cognito app client. No `userPoolAppSecret` is used
 *     or stored anywhere in this function.
 *   - CSRF protection uses a nonce-signing secret (`config.csrfNonceSecret`).
 *     Per ARCC SAX-02 (secrets management) this secret MUST NOT be committed to
 *     git; it is injected at synth from an out-of-band source (see task 8).
 *
 * The handler makes no AWS SDK / IAM calls: the OAuth2 authorization-code
 * exchange is a direct HTTPS call from the edge to the Cognito token endpoint.
 * Its execution role therefore needs only CloudWatch Logs (ARCC SAX-08, least
 * privilege) - enforced in the CDK stack, not here.
 */

const { Authenticator } = require("cognito-at-edge");

// Prefer the synth-injected, gitignored sidecar (real Cognito IDs + CSRF
// secret) over the committed empty placeholder. See spec 029 decision D3-A and
// the CDK stack's config-injection step.
let config;
try {
  config = require("./config.local.json");
} catch (err) {
  config = require("./config.json");
}

// Construct the Authenticator once per execution environment and reuse it
// across warm invocations - the library's intended usage, and the way to keep
// per-request latency low at the edge. With the empty placeholder config this
// module is not exercised (real config is injected before deploy).
const authenticator = new Authenticator({
  region: config.region,
  userPoolId: config.userPoolId,
  userPoolAppId: config.userPoolAppId,
  // D2-A public client: intentionally no `userPoolAppSecret`.
  userPoolDomain: config.userPoolDomain,
  // Parse the OAuth2 authorization-code response on the Hosted UI callback path.
  parseAuthPath: "/oauth2/idpresponse",
  // Scope auth cookies to the whole site. Without this, cookies set on the
  // /oauth2/idpresponse callback response inherit RFC 6265 default-path
  // "/oauth2" and are NOT sent for the dashboard at "/", causing an infinite
  // login redirect loop. cognito-at-edge's README requires cookiePath whenever
  // parseAuthPath is set. (spec 029 live-validation fix, 2026-07-27)
  cookiePath: "/",
  // On the default *.cloudfront.net domain emit host-only cookies (no Domain
  // attribute): cloudfront.net is on the Public Suffix List, so a Domain-scoped
  // cookie on that host is unreliable across browsers. Host-only is correct for
  // a single CloudFront distribution.
  disableCookieDomain: true,
  // CSRF protection (signed state nonce). The signing secret is injected via
  // config and is never committed to git. Enabled only when the secret is set
  // so the empty placeholder config stays constructible for local checks.
  ...(config.csrfNonceSecret
    ? { csrfProtection: { nonceSigningSecret: config.csrfNonceSecret } }
    : {}),
});

// Lambda@Edge viewer-request entry point. `cognito-at-edge` accepts the full
// CloudFront event and extracts the request internally.
//
// TM-16 (audit trail): after the library has AUTHENTICATED the viewer, stamp the
// manager's Cognito `sub` onto the origin request so the backend can attribute a
// tool invocation to a person. Notes on why it is done this way:
//
//   * The identity is taken from the ID token the library just VERIFIED. The
//     origin therefore does not need to verify it again, which is the point of
//     doing this at the auth boundary rather than in the backend.
//   * The header is set UNCONDITIONALLY, overwriting anything the client sent.
//     The distribution uses OriginRequestPolicy.ALL_VIEWER, so a client-supplied
//     `x-manager-sub` would otherwise be forwarded verbatim and be
//     indistinguishable from ours -- i.e. trivially spoofable. Overwriting is
//     what makes the header trustworthy.
//   * `sub` is an opaque pseudonymous id. The email/username claims are
//     deliberately NOT forwarded: an audit log should not accumulate PII when a
//     stable opaque id answers "who did this".
//   * Everything is wrapped so that ANY failure degrades to "no identity" and
//     never breaks login. Authentication is the job here; attribution is the
//     bonus, and the bonus must not be able to take down the job.
const MANAGER_SUB_HEADER = "x-manager-sub";

/** Extract `sub` from an already-verified Cognito ID token cookie. */
function extractSub(request) {
  const cookieHeaders = (request.headers && request.headers.cookie) || [];
  for (const { value } of cookieHeaders) {
    for (const pair of String(value).split(";")) {
      const eq = pair.indexOf("=");
      if (eq < 0) continue;
      const name = pair.slice(0, eq).trim();
      if (!name.endsWith(".idToken")) continue;
      const jwt = pair.slice(eq + 1).trim();
      const parts = jwt.split(".");
      if (parts.length !== 3) continue;
      // Signature verification already happened in `authenticator.handle`; this
      // only reads a claim out of the token that was accepted. Parsing is guarded
      // per-cookie so one malformed token cannot abort the scan (or throw): a
      // garbled cookie should mean "no attribution", never a failed request.
      try {
        const payload = JSON.parse(
          Buffer.from(parts[1].replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"),
        );
        if (payload && typeof payload.sub === "string" && payload.sub) return payload.sub;
      } catch (err) {
        continue;
      }
    }
  }
  return null;
}

exports.handler = async (event) => {
  const result = await authenticator.handle(event);

  // A redirect/response (unauthenticated) has a `status`; only a forwarded
  // REQUEST can carry headers to the origin.
  if (!result || typeof result.status !== "undefined" || !result.headers) {
    return result;
  }

  try {
    const sub = extractSub(result);
    // Always assign, even when null -> [] , so a client-supplied value is
    // replaced rather than passed through.
    result.headers[MANAGER_SUB_HEADER] = sub
      ? [{ key: "X-Manager-Sub", value: sub }]
      : [];
    if (!sub) delete result.headers[MANAGER_SUB_HEADER];
  } catch (err) {
    // Never let attribution break authentication.
    delete result.headers[MANAGER_SUB_HEADER];
  }
  return result;
};

exports.extractSub = extractSub;
exports.MANAGER_SUB_HEADER = MANAGER_SUB_HEADER;
