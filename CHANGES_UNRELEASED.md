**See [the release process docs](docs/howtos/cut-a-new-release.md) for the steps to take when cutting a new release.**

# Unreleased Changes

[Full Changelog](https://github.com/mozilla/application-services/compare/v0.32.3...master)

## General

- All of our cryptographic primitives are now backed by NSS. This change should be transparent our customers.  
If you build application-services, it is recommended to delete the `libs/{desktop, ios, android}` folders and start over using `./build-all.sh [android|desktop|ios]`.

## Push

### Breaking Changes

- `OpenSSLError` has been renamed to the more general `CryptoError`.

## FxAClient

### Breaking Changes

- iOS: FirefoxAccountError enum variants have their name `lowerCamelCased`
  instead of `UpperCamelCased`, to better fit with common Swift code style.
  ([#1324](https://github.com/mozilla/application-services/issues/1324))
