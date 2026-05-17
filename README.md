# lilical

A multi-backend calendar for the Linux desktop.

Read AND write across **Google Calendar**, **Outlook/Microsoft 365**, and **CalDAV** in one app, with a dense Business-Calendar-inspired UI.

## Quick start

```bash
pixi install
pixi run migrate
pixi run run
```

## Development

```bash
pixi run test     # run tests
pixi run lint     # ruff check
pixi run fmt      # ruff format
pixi run typecheck  # basedpyright
```

## Releases

To cut a release: `git tag v0.2.0 && git push origin v0.2.0`. CI builds the
AppImage and publishes it to GitHub Releases automatically. The
`OAUTH_CREDENTIALS_JSON` repo secret must be set; without it the Google backend
in the released AppImage will be non-functional.

For a test run before tagging for real, push a pre-release tag
(`v0.0.0-test`) — `workflow_dispatch` in the Actions UI also works and
uploads the artifact without creating a release.

## License

GPL-3.0-or-later
