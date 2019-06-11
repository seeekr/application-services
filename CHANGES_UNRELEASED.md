**See [the release process docs](docs/howtos/cut-a-new-release.md) for the steps to take when cutting a new release.**

# Unreleased Changes

[Full Changelog](https://github.com/mozilla/application-services/compare/v0.31.1...master)

## FxA Client

### What's new

- The OAuth access token cache is now persisted as part of the account state data,
  which should reduce the number of times callers need to fetch a fresh access token
  from the server.
