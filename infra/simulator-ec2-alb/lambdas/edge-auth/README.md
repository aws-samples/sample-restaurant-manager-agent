# edge-auth - Lambda@Edge Cognito authenticator (spec 029)

A Node.js **Lambda@Edge** function (viewer-request) that enforces Amazon Cognito
Hosted UI login at the CloudFront edge using the awslabs
[`cognito-at-edge`](https://github.com/awslabs/cognito-at-edge) library. It
replaces spec 007's ALB `authenticate-cognito` listener action with no
application code change. Deployed by `SimulatorEc2AlbStack` via
`cloudfront.experimental.EdgeFunction` (the function is created in `us-east-1`,
as Lambda@Edge requires).

## Files

| File | Purpose |
|------|---------|
| `index.js` | Handler: builds the `cognito-at-edge` `Authenticator` from `config.json` and delegates the viewer-request event to `authenticator.handle`. |
| `config.json` | **Committed as an empty placeholder.** The real Cognito IDs + CSRF nonce secret are written here at CDK **synth** time from context (decision D3-A). |
| `package.json` | Pins `cognito-at-edge` (exact version). |
| `package-lock.json` | Committed for reproducible `npm ci` (re-included past the parent `.gitignore`). |
| `bundle.sh` | Vendors `node_modules/` via `npm ci --omit=dev` before `cdk synth`/`deploy`. |

## Config contract (`config.json`)

```json
{
  "region": "<cognito user pool region, e.g. us-west-2>",
  "userPoolId": "<user pool id>",
  "userPoolAppId": "<public app client id>",
  "userPoolDomain": "<prefix>.auth.<region>.amazoncognito.com",
  "csrfNonceSecret": "<CSRF state nonce signing secret - NEVER committed>"
}
```

- **Public app client (D2-A):** no `userPoolAppSecret` is used or stored.
- **`csrfNonceSecret`** is a signing secret and, per ARCC SAX-02 (secrets
  management), must **not** be committed to git - it is injected at synth from
  an out-of-band source (see spec 029 task 8).

## Build / deploy

```bash
./bundle.sh            # vendor node_modules (pure JS; no container needed)
```

Then deploy `SimulatorEc2AlbStack` (two-phase for the first rollout - see the
stack README and spec 029 design.md).

## Why no environment variables?

Lambda@Edge forbids environment variables, so configuration is read from the
bundled `config.json` instead of `process.env`. This function makes **no AWS
SDK / IAM calls** (the OAuth2 code exchange is a direct HTTPS call to the
Cognito token endpoint), so its execution role needs only CloudWatch Logs
(ARCC SAX-08, least privilege).
