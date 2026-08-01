# Changelog

All notable changes to this project will be documented in this file.

Release Please maintains this file from Conventional Commit messages.

## [0.6.0](https://github.com/adrighem/ha-domoticz-sync/compare/v0.5.0...v0.6.0) (2026-08-01)


### Features

* **bridge:** map Domoticz command callbacks to universal HA services under the signed control protocol ([605b675](https://github.com/adrighem/ha-domoticz-sync/commit/605b67581e479ec9526ea5d474c68e39bcef2cb6))
* **core:** model compound measurements and support their serialization ([b91aacb](https://github.com/adrighem/ha-domoticz-sync/commit/b91aacb2de63685a09b69bbc2e0fe7a542e0c04d))
* define continuous export semantics ([e09360e](https://github.com/adrighem/ha-domoticz-sync/commit/e09360e25c7bcedea5fee747f5ccc42ff4539e63))
* design authenticated Domoticz inventory ([9b236d7](https://github.com/adrighem/ha-domoticz-sync/commit/9b236d75db3f8dd0a2d4cd1f3e5fb3a6527cadc7))
* **plugin:** add native Temp+Hum compound-device profile, codec, and partial availability tests ([262a22b](https://github.com/adrighem/ha-domoticz-sync/commit/262a22b493c7bf12790b505ef65bd37550be60b7))
* **protocol:** implement signed reverse commands, threat model, message schemas, and unit tests ([8088e04](https://github.com/adrighem/ha-domoticz-sync/commit/8088e043fa5c9333e8546b360f0ff06bb72c61a9))
* reconcile against authenticated Domoticz inventory ([c3ac10e](https://github.com/adrighem/ha-domoticz-sync/commit/c3ac10ea96c3f506e5556d6d64426171c77a37ae))
* repair safe Domoticz drift ([ff075cc](https://github.com/adrighem/ha-domoticz-sync/commit/ff075cc9db9d0657faa1fe3a594de760d008e92a))
* subscribe to Home Assistant export changes ([03b810a](https://github.com/adrighem/ha-domoticz-sync/commit/03b810a81752907abee68ca45400ba313e377ed6))
* synchronize exports continuously ([99dae55](https://github.com/adrighem/ha-domoticz-sync/commit/99dae5516b5e0516964316c4b7be74142e492f53))


### Bug Fixes

* resolve CodeQL findings ([1d28d44](https://github.com/adrighem/ha-domoticz-sync/commit/1d28d444740079cd486bf2dbd44115e3b3b4b78c))


### Documentation

* add rolling upgrade verification ([621bacc](https://github.com/adrighem/ha-domoticz-sync/commit/621bacc4daa4c40aa8036f2e9fba6b9ca426363d))
* complete binary sensor verification ([cbf30b0](https://github.com/adrighem/ha-domoticz-sync/commit/cbf30b0cba63ceeedffd5c42616393ed2328edbc))
* complete Domoticz inventory verification ([12bb9ef](https://github.com/adrighem/ha-domoticz-sync/commit/12bb9efc63eef5358463d7ebb8334ea97c52450e))
* complete rolling upgrade verification ([d5ab4d4](https://github.com/adrighem/ha-domoticz-sync/commit/d5ab4d485221f2ecd167f0f054e498d5f3ee95a5))
* **conductor:** Synchronize docs for track 'Complete Home Assistant to Domoticz export roadmap' ([1efb9ae](https://github.com/adrighem/ha-domoticz-sync/commit/1efb9ae81ad819b715ccc4e13d4b919ecf18bcc0))
* link binary verification evidence ([c4868fd](https://github.com/adrighem/ha-domoticz-sync/commit/c4868fd33e1d6b227eda2ff344ebc9d0d31b6689))
* link current release evidence ([5e4169a](https://github.com/adrighem/ha-domoticz-sync/commit/5e4169a0e439d360ff6c77eb5336a1754bfd3adb))
* link live verification evidence ([3068a1e](https://github.com/adrighem/ha-domoticz-sync/commit/3068a1eb501c8f80084a85e3e05e76dc96228db4))
* link rolling verification evidence ([787d49b](https://github.com/adrighem/ha-domoticz-sync/commit/787d49b32b8eb9516eb6fb5e92baa153019dce48))
* record continuous export semantics ([c220106](https://github.com/adrighem/ha-domoticz-sync/commit/c220106918f194bc921be5b6a85de2ac409b13dd))
* record continuous synchronization progress ([0234935](https://github.com/adrighem/ha-domoticz-sync/commit/0234935d0d3d6ff6475c3219fe3c51ac3749c28d))
* record current matching release ([7b44010](https://github.com/adrighem/ha-domoticz-sync/commit/7b4401094f0597af45cc30a2ed5a400e86a0e64a))
* record Domoticz inventory reconciliation ([dee9d10](https://github.com/adrighem/ha-domoticz-sync/commit/dee9d10b12329cb2bfce36055bd0ac806ecbee24))
* record Home Assistant change subscription ([a9a006b](https://github.com/adrighem/ha-domoticz-sync/commit/a9a006b206f48e7d11f11812b1987ae8d02bfc02))
* record inventory protocol design ([afa9929](https://github.com/adrighem/ha-domoticz-sync/commit/afa99291a53116fade946bbb18acd35d1dd618b7))
* record live continuous synchronization verification ([bc21ef6](https://github.com/adrighem/ha-domoticz-sync/commit/bc21ef640bb9a96b677015b3a16b0d56e772656d))
* record live export verification ([7347ca3](https://github.com/adrighem/ha-domoticz-sync/commit/7347ca340ec8e568ca445af97824ff4f41a6390d))
* record safe Domoticz drift repair ([99b631d](https://github.com/adrighem/ha-domoticz-sync/commit/99b631d937f639b8f7872a9ffc52543be5e8b745))
* record verification progress ([b5857b2](https://github.com/adrighem/ha-domoticz-sync/commit/b5857b2514e1e61912fb02a3cddc5b645f37c908))

## [0.5.0](https://github.com/adrighem/ha-domoticz-sync/compare/v0.4.0...v0.5.0) (2026-07-30)


### Features

* export passive binary sensors ([d5bf19d](https://github.com/adrighem/ha-domoticz-sync/commit/d5bf19d8b8c97840573b5c50e2f438ab10c68b40))


### Bug Fixes

* harden sync reliability and compatibility ([#20](https://github.com/adrighem/ha-domoticz-sync/issues/20)) ([84831bd](https://github.com/adrighem/ha-domoticz-sync/commit/84831bd6c6f8a40dcbd90108a652b881fd8dbaf9))


### Documentation

* recommend PyPluginStore installation ([ef9d4b0](https://github.com/adrighem/ha-domoticz-sync/commit/ef9d4b0fc2cfa2a80ed7463d013787a4c5ef48df))
* record 0.4.0 release ([91a3cba](https://github.com/adrighem/ha-domoticz-sync/commit/91a3cbaf4e8ec4c4d8dad4d345e5409b9e5e51c9))
* record passive binary progress ([d8f0b92](https://github.com/adrighem/ha-domoticz-sync/commit/d8f0b92509f5c623203526108572733c5d5d7d19))

## [0.4.0](https://github.com/adrighem/ha-domoticz-sync/compare/v0.3.1...v0.4.0) (2026-07-30)


### Features

* complete numeric export mapping coverage ([05a8b5f](https://github.com/adrighem/ha-domoticz-sync/commit/05a8b5f08b901356eadfc96c259d0a4d5dff39c5))
* warn for excluded export labels ([b5b9cdc](https://github.com/adrighem/ha-domoticz-sync/commit/b5b9cdc2b195e1fbc5dd96a5b770729e03df6ffe))


### Documentation

* update export roadmap progress ([00af771](https://github.com/adrighem/ha-domoticz-sync/commit/00af7715b350a8defde6b228be1f5c3bd6e0c44e))

## [0.3.1](https://github.com/adrighem/ha-domoticz-sync/compare/v0.3.0...v0.3.1) (2026-07-30)


### Bug Fixes

* support Home Assistant 2026.7 entity categories ([7ac1e0f](https://github.com/adrighem/ha-domoticz-sync/commit/7ac1e0fbdbeca4952ef95bee4e586b52b7a4cf81))

## [0.3.0](https://github.com/adrighem/ha-domoticz-sync/compare/v0.2.0...v0.3.0) (2026-07-30)


### Features

* map sensors to native Domoticz types ([#15](https://github.com/adrighem/ha-domoticz-sync/issues/15)) ([934a3bc](https://github.com/adrighem/ha-domoticz-sync/commit/934a3bcae0ad068173a7548e9e34da4e046b27ef))
* negotiate compatible bridge protocols ([#16](https://github.com/adrighem/ha-domoticz-sync/issues/16)) ([0df41bd](https://github.com/adrighem/ha-domoticz-sync/commit/0df41bd388b352b2c05e52d96649048726829ccb))
* sync numeric Home Assistant entities to Domoticz ([#13](https://github.com/adrighem/ha-domoticz-sync/issues/13)) ([f29f0cf](https://github.com/adrighem/ha-domoticz-sync/commit/f29f0cf07203a48e6e76a0f6078a7e4869e02f0f))

## [0.2.0](https://github.com/adrighem/ha-domoticz-sync/compare/v0.1.4...v0.2.0) (2026-07-29)


### Features

* add authenticated Domoticz companion bridge ([#11](https://github.com/adrighem/ha-domoticz-sync/issues/11)) ([b114fdb](https://github.com/adrighem/ha-domoticz-sync/commit/b114fdbd774acaca264ce0ad1e7bcea5a8edad8c))

## [0.1.4](https://github.com/adrighem/ha-domoticz-sync/compare/v0.1.3...v0.1.4) (2026-07-28)


### Bug Fixes

* complete local brand asset support ([26464a9](https://github.com/adrighem/ha-domoticz-sync/commit/26464a9aed81766dff214c4dedf9c48abe384ad3))

## [0.1.3](https://github.com/adrighem/ha-domoticz-sync/compare/v0.1.2...v0.1.3) (2026-07-25)


### Documentation

* record maintainer run for PR 4 ([bbcedd6](https://github.com/adrighem/ha-domoticz-sync/commit/bbcedd6857dca59f9302d0756c0aeb4a2b5a3c83)), closes [#4](https://github.com/adrighem/ha-domoticz-sync/issues/4)
* record maintainer run for PR 8 and PR 7 ([0c16c26](https://github.com/adrighem/ha-domoticz-sync/commit/0c16c26813b30566dbf180ef107bd1f22c6eeff9))

## [0.1.2](https://github.com/adrighem/ha-domoticz-sync/compare/v0.1.1...v0.1.2) (2026-07-04)


### Bug Fixes

* prevent redundant generic fallback Value entities when primary metrics exist ([ad086fb](https://github.com/adrighem/ha-domoticz-sync/commit/ad086fb28138abc0677aba4c12239d80fc3f3bb6))


### Documentation

* guide users on syncing selected devices via dedicated user ([9fe1511](https://github.com/adrighem/ha-domoticz-sync/commit/9fe1511fedd589ff114c8446846ef089fe1b4036))
* update installation instructions to be based on HACS ([6549365](https://github.com/adrighem/ha-domoticz-sync/commit/654936579bce91b112bfdd711d1d392fd3571a51))

## [0.1.1](https://github.com/adrighem/ha-domoticz-sync/compare/v0.1.0...v0.1.1) (2026-07-04)


### Bug Fixes

* correct brand asset location and remove extra domains key in hacs.json ([b68fbfe](https://github.com/adrighem/ha-domoticz-sync/commit/b68fbfebcca8b35f1ef1e932e0d85c9d8ff5841c))
* preserve custom subpath in normalized base URL ([6e28640](https://github.com/adrighem/ha-domoticz-sync/commit/6e286409a861fee56959dd2178004a1d669587d9))


### Documentation

* add app icon ([14bd7c6](https://github.com/adrighem/ha-domoticz-sync/commit/14bd7c6df874b5cdafb999f2bb891a1a032aab8f))
* add Domoticz Sync app icon to README ([9c46d1f](https://github.com/adrighem/ha-domoticz-sync/commit/9c46d1f1d32165607dcaa7c2323b722f7c472aa7))

## 0.1.0 - 2026-07-03

- Initial Domoticz Sync custom integration scaffold.
