# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub security advisories: open the repository's **Security** tab
and choose **Report a vulnerability**. Do not open a public issue for a security problem.

Include what you can of: the affected version or commit, the configuration, the request that triggers the
problem, and what you expected instead. Do not include real keys or real request content; a synthetic
reproduction is enough.

You should get a first answer within a week. Fixes are released as a new version and noted in
`CHANGELOG.md`.

## Supported versions

Only the latest release receives fixes.

## Scope

In scope: anything that lets a caller bypass the key or the `Host` check, exceed the documented caps or
limits, make request or response text reach a log, a metric, a stats document, an error body or the state
directory, or read a key value through any output.

Out of scope: exposing the gateway beyond loopback, and weaknesses of the backend servers behind it.
