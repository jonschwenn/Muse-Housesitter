# Public communication guide

Use this guide when describing Housesitter outside the repository. The
goal is to make the maintainer, intended integration, current capabilities,
and privacy trade-offs clear at first contact. See
[ATTRIBUTION.md](../ATTRIBUTION.md) for upstream credits and references.

## Reusable wording

Suggested GitHub About description:

> Independent, unofficial tool for read-only home measurements, designed
> for Meta's Muse gadget SDK. Personal project by Jon Schwenn.

Short notice for a release, demo caption, or project page:

> Muse Housesitter is an independent personal project by Jon Schwenn. It
> is not affiliated with, sponsored by, endorsed by, or maintained by
> Meta Platforms, Inc. or its affiliates. Muse and Meta names and marks
> belong to Meta Platforms, Inc. or its affiliates.

Opening for a launch post:

> I'm building Muse Housesitter, an independent, unofficial tool designed
> to give Muse read-only measurements from my home network through the
> gadget SDK. The core command infrastructure is implemented; the first
> UniFi data module is still planned. Meta and Ubiquiti do not sponsor,
> endorse, maintain, or support this project. Results sent to Muse leave
> the Pi, so review the upstream terms and privacy policy before using it.

Update the capability sentence against the current README before each
release. Link the README and attribution page. Keep the independence
sentence next to the project name so it survives excerpts and reposts.

## Before sharing more widely

- Put the short notice in announcements, release descriptions, and video
  descriptions. Include it in a demo's opening frame or caption; viewers
  may never open the repository. These are publishing steps for the
  maintainer, not claims that those surfaces have already been updated.
- Use Jon's account and this repository as the source of the project.
  Keep artwork original. Avoid Meta or Muse logos, avatars, official
  badges, and layouts that resemble an official product announcement.
  Use plain text for upstream names. “Official,” “certified,” “partner,”
  and “approved” need evidence and permission; SDK compatibility alone
  does not establish any of them.
- Check each capability claim against shipped code. Label future modules
  and integrations as planned. Use invented fixture data for demos and
  screenshots; never share credentials or real house data.
- Explain the boundary: Housesitter's commands are read-only toward the
  systems they observe. The SDK can give Muse other capabilities on the
  Pi, and results returned to Muse leave the Pi. Avoid claims that the
  whole setup is offline, anonymous, or unable to change anything.
- Keep Housesitter support in this repository. Don't invite people to
  ask Meta or Ubiquiti to support it. Follow community posting rules and
  describe it as the maintainer's project when sharing it.
- Link the applicable upstream terms and privacy policy. Keep software
  licensing, service access, and brand permissions distinct. Never include
  credentials with a release or demo.

## Responses to common questions

**“Is this made by Meta?”**

> No. I maintain Housesitter independently. It is designed to work with
> Meta's Muse through the separately installed gadget SDK. Meta does not
> sponsor, endorse, maintain, or support Housesitter. The README links
> the upstream SDK and explains the relationship.

**“Does my home data stay on the Pi?”**

> Results returned to Muse through the SDK leave the Pi for Muse's
> service. They can include device and network identifiers. Read-only
> access limits changes to the observed systems; it does not keep the
> returned data local. Review the upstream terms and privacy policy
> before connecting your network.

**“Is this AI-generated?”**

> This is a human-run project that uses AI tools for development,
> testing, and review. Jon decides what is accepted and merged. The
> repository documents its review gates and credits agent contributions.

Use these replies only when they accurately describe the situation. If a
report identifies a real error, correct the original page or caption as
well as replying. Keep the explanation factual; do not argue about the
reporter's motives or make broad claims about Meta's practices.
