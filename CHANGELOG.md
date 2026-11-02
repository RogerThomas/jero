# Changelog

## [0.3.0](https://github.com/RogerThomas/jero/compare/0.2.0...0.3.0) (2026-11-02)


### ⚠ BREAKING CHANGES

* prefix internal modules with _ ([#94](https://github.com/RogerThomas/jero/issues/94))

### Bug Fixes

* report TYPE_CHECKING-only annotations as a WiringError, and document why ([#92](https://github.com/RogerThomas/jero/issues/92)) ([7231789](https://github.com/RogerThomas/jero/commit/72317894e50b1c886adc4df64920754748393dca))
* require an authorization header binding for a declared http security scheme ([#91](https://github.com/RogerThomas/jero/issues/91)) ([56f098d](https://github.com/RogerThomas/jero/commit/56f098d57a2c66d18518104919fb46797373dd99))


### Code Refactoring

* prefix internal modules with _ ([#94](https://github.com/RogerThomas/jero/issues/94)) ([4fcec4c](https://github.com/RogerThomas/jero/commit/4fcec4c270881f096ff8a29034e5817fbedafea8))

## [0.2.0](https://github.com/RogerThomas/jero/compare/0.1.13...0.2.0) (2026-11-02)


### ⚠ BREAKING CHANGES

* BearerAuth/BasicAuth no longer inherit the Auth protocol ([#89](https://github.com/RogerThomas/jero/issues/89))

### Bug Fixes

* BearerAuth/BasicAuth no longer inherit the Auth protocol ([#89](https://github.com/RogerThomas/jero/issues/89)) ([4684996](https://github.com/RogerThomas/jero/commit/4684996f0b482d3fae716fce04a7ee85e9009633))

## [0.1.13](https://github.com/RogerThomas/jero/compare/0.1.12...0.1.13) (2026-11-02)


### Bug Fixes

* let CORS compose with route-covering middleware ([#87](https://github.com/RogerThomas/jero/issues/87)) ([037f1d5](https://github.com/RogerThomas/jero/commit/037f1d5457576fa1b8840b828332c438540ec084))

## [0.1.12](https://github.com/RogerThomas/jero/compare/0.1.11...0.1.12) (2026-11-02)


### Features

* tagged-union json bodies and {slot}:verb custom-method paths ([#84](https://github.com/RogerThomas/jero/issues/84)) ([107611b](https://github.com/RogerThomas/jero/commit/107611bb2502fb34a6d665d92ed7c02fab5f4970))


### Bug Fixes

* never fail reversal at request time, and encode only unsafe slot values ([#86](https://github.com/RogerThomas/jero/issues/86)) ([e42d0fd](https://github.com/RogerThomas/jero/commit/e42d0fd3a7ae57a229208eb2fa88dfd8f59d5bda))

## [0.1.11](https://github.com/RogerThomas/jero/compare/0.1.10...0.1.11) (2026-10-29)


### Features

* let authenticate return any user type ([#80](https://github.com/RogerThomas/jero/issues/80)) ([07bd302](https://github.com/RogerThomas/jero/commit/07bd302f3aad79643761fc81bab4a8e9f2314222))

## [0.1.10](https://github.com/RogerThomas/jero/compare/0.1.9...0.1.10) (2026-10-28)


### Bug Fixes

* item routes take exactly one id path field, plus docs accuracy fixes ([#78](https://github.com/RogerThomas/jero/issues/78)) ([501f91f](https://github.com/RogerThomas/jero/commit/501f91fbbf6992a6191a9698f25ed797f3394cb2))

## [0.1.9](https://github.com/RogerThomas/jero/compare/0.1.8...0.1.9) (2026-10-25)


### Documentation

* correct the FastAPI benchmark rows and describe POST /movies as JWT + validation ([#76](https://github.com/RogerThomas/jero/issues/76)) ([55a3402](https://github.com/RogerThomas/jero/commit/55a3402f11adfc9c6ceee78d42b12fe199abddfd))

## [0.1.8](https://github.com/RogerThomas/jero/compare/0.1.7...0.1.8) (2026-10-25)


### Documentation

* update benchmarks to the single full-fleet run of 2026-10-25 ([#74](https://github.com/RogerThomas/jero/issues/74)) ([7d5d2d6](https://github.com/RogerThomas/jero/commit/7d5d2d6149ffc7478589352183c9bfa241324168))

## [0.1.7](https://github.com/RogerThomas/jero/compare/0.1.6...0.1.7) (2026-10-25)


### Documentation

* tighten the benchmark page and scope cross-language claims to one core ([#72](https://github.com/RogerThomas/jero/issues/72)) ([861c408](https://github.com/RogerThomas/jero/commit/861c408590c59687d7381a6ec9de9740803a06bf))

## [0.1.6](https://github.com/RogerThomas/jero/compare/0.1.5...0.1.6) (2026-10-23)


### Documentation

* update benchmarks to the wrk/peregrine runs of 2026-10-23 ([#70](https://github.com/RogerThomas/jero/issues/70)) ([a140796](https://github.com/RogerThomas/jero/commit/a140796b06762c8d0a0de6e64e076d2c51e63378))

## [0.1.5](https://github.com/RogerThomas/jero/compare/0.1.4...0.1.5) (2026-10-23)


### Bug Fixes

* close sync resources and report startup failure when an async close fails ([#68](https://github.com/RogerThomas/jero/issues/68)) ([38a848d](https://github.com/RogerThomas/jero/commit/38a848d9772bfa75db79e454a0548118c7d8a1c7))

## [0.1.4](https://github.com/RogerThomas/jero/compare/0.1.3...0.1.4) (2026-10-22)


### Features

* support Python 3.15 ([#65](https://github.com/RogerThomas/jero/issues/65)) ([0b32355](https://github.com/RogerThomas/jero/commit/0b3235577f489a5a9f385bfe80acfe73c4c3c5be))

## [0.1.3](https://github.com/RogerThomas/jero/compare/0.1.2...0.1.3) (2026-09-28)


### Features

* accept url-encoded bodies for file-free forms ([#57](https://github.com/RogerThomas/jero/issues/57)) ([a0f4f98](https://github.com/RogerThomas/jero/commit/a0f4f9854f7ec2c737d08f805f6dc9281d221188))

## [0.1.2](https://github.com/RogerThomas/jero/compare/0.1.1...0.1.2) (2026-09-28)


### Features

* add content_stream body source ([#55](https://github.com/RogerThomas/jero/issues/55)) ([643daf4](https://github.com/RogerThomas/jero/commit/643daf4a9d44d92b15ff9a6ff4e5e6115317e408))
