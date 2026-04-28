# Contributing

## Commit message convention (ironic)

This project removes AI attributions from other people's repos. Commits to
**this** repo do the opposite: they keep a Claude attribution, plus a winky emoji.

Every commit message in this repository should end with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code) 😉

Co-Authored-By: Claude <noreply@anthropic.com>
```

Yes, on purpose. Don't strip it.

## Development

This project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra build         # install runtime + PyInstaller
uv run aiscrub --help         # run from source
uv run pyinstaller --onefile --name aiscrub aiscrub.py
```

## Releases

Tag a version (`vX.Y.Z`) and push the tag. The `release` workflow builds a
PyInstaller binary on Linux, macOS, and Windows, zips each one, and publishes a
GitHub Release with the zips and a `SHA256SUMS.txt` attached.
