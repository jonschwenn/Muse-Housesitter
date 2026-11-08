# Attribution and independence

Muse Housesitter is an independent, unofficial project maintained by Jon
Schwenn. It is not affiliated with, sponsored by, endorsed by, or maintained
by Meta Platforms, Inc. or its affiliates, Ubiquiti Inc., or the Open Home
Foundation. These organizations do not provide support for Housesitter.

## Names and upstream work

- **Muse and Meta:** the names and marks belong to Meta Platforms, Inc. or
  its affiliates. In this project, “Muse” identifies Meta's service and the
  intended integration with its gadget SDK.
- **Muse gadget SDK:** credit goes to Meta and the contributors to
  [`facebookincubator/muse-gadget-sdk`](https://github.com/facebookincubator/muse-gadget-sdk).
  Housesitter uses the Linux SDK's documented drop-in command and CLI
  interfaces. The SDK is installed separately; Housesitter does not import
  `musegadget` or include the SDK in its runtime dependencies.
- **UniFi and Ubiquiti:** names and marks belong to Ubiquiti Inc. They
  identify the controller and equipment addressed by the first planned
  module.
- **Home Assistant:** the name and logo belong to the Open Home Foundation.
  References describe possible future work. No Home Assistant module is
  implemented in Housesitter.

Other third-party names and marks belong to their respective owners.
References are for identification and technical context and imply no
endorsement. This notice grants no rights to third-party names, logos, or
other brand assets.

## Software licenses and service access are separate

Housesitter's code is MIT-licensed; see [`LICENSE`](LICENSE). This notice
does not modify that license or relicense anyone else's work.

The separately installed gadget SDK has its own
[Apache 2.0 license](https://github.com/facebookincubator/muse-gadget-sdk/blob/main/LICENSE).
Third-party software remains subject to its applicable license, including
any required copyright and attribution notices. Software licenses and
permissions to use third-party branding are separate; see the SDK license's
section 6 and [Meta's trademark guidance](https://www.meta.com/brand/resources/meta/our-trademarks/).

Connecting to Muse is also subject to the
[Gadget SDK Token Terms](https://gadgets.muse.ai/sdk-terms) and the
[Muse terms](https://muse.ai/terms). The SDK's source-code license does not
itself grant service access. Each user must obtain their own credentials;
never distribute a maintainer's credentials with Housesitter.

Results returned to Muse through the SDK leave the Pi for Muse's service.
Review the [Muse privacy policy](https://muse.ai/privacy) before connecting.

## Questions and corrections

For Housesitter bugs or attribution corrections, open an issue in
[`jonschwenn/Muse-Housesitter`](https://github.com/jonschwenn/Muse-Housesitter/issues).
Use invented examples and do not post secrets or real house data. Questions
about an upstream service or SDK belong with that provider's support or
repository.

## References

Upstream references, checked on **2026-11-08**:

- [Muse Gadgets](https://gadgets.muse.ai/).
- [The Linux SDK README](https://github.com/facebookincubator/muse-gadget-sdk/blob/main/linux/README.md)
  for installation and command extensions.
- [The SDK license](https://github.com/facebookincubator/muse-gadget-sdk/blob/main/LICENSE),
  including redistribution and trademark provisions.
- [Gadget SDK Token Terms](https://gadgets.muse.ai/sdk-terms) for service
  access and credential requirements.
- [Meta's trademark guidance](https://www.meta.com/brand/resources/meta/our-trademarks/).
- [Ubiquiti's UniFi Controller terms, section I(f)](https://www.ui.com/legal/unifi/termsofservice/?direct=true)
  for trademark attribution.
- [Home Assistant's name and logo guidance](https://contributors.home-assistant.io/brand/name-and-logo/)
  for ownership and brand guidelines.

The Muse terms and privacy pages required a login during this review; their
contents were not independently verified. Users should read the versions
presented to their own accounts. Linked policies can change.
