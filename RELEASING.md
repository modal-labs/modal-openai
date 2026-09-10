# Publishing modal-openai

The release workflow builds and checks the distributions, tests installation
without private GitHub access, then publishes through PyPI Trusted Publishing.
No long-lived PyPI token is needed.

## One-time setup

In [PyPI publishing settings](https://pypi.org/manage/account/publishing/),
register a pending GitHub publisher:

- PyPI project: `modal-openai`
- Repository owner: `modal-labs`
- Repository: `modal-openai`
- Workflow: `workflow.yml`
- Environment: `pypi`

Create the matching GitHub environment `pypi`. A pending publisher does not
reserve the package name; the first successful upload creates the project.

The project is distributed under the [MIT license](LICENSE).

## Release

1. Run the CONTRIBUTING.md development checks with preview SDK access. Public CI skips
   the SDK-dependent test modules; it still tests package installation and the
   remaining integration code.
2. Update `pyproject.toml` and `modal_agents/__init__.py` to the release version.
3. Commit and push the release changes; wait for CI to pass.
4. Publish a GitHub release tagged `vVERSION` (for example, `v0.1.0`). This triggers
   `.github/workflows/workflow.yml`. Do not reuse an uploaded version.
5. Confirm installation from PyPI in a clean environment and update the README
   installation instructions once the first release is available.

The private preview SDK is a development dependency group, not wheel metadata:
PyPI rejects direct Git URL dependencies. Users install that SDK separately to
select/create agents or run smoke tests. Do not bundle or republish the SDK.
