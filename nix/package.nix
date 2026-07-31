{
  lib,
  python3Packages,
  makeWrapper,
  beads,
  git,
  # `bd` and `git` are runtime dependencies (the server shells out to both).
  # They are appended to PATH rather than prepended — see postInstall.
  preferSystemBd ? true,
}:

python3Packages.buildPythonApplication {
  pname = "claude-mailbox";
  # Read from pyproject rather than duplicated here, so a version bump is one
  # edit and the flake can't silently drift from the wheel.
  version = (lib.importTOML ../pyproject.toml).project.version;
  pyproject = true;

  # Only what the build and test phases actually need. Notably excludes .beads/
  # — the package must not carry a beads workspace into the store, since an
  # installed build resolves its workspace per-user at runtime (see bd.py's
  # _default_workspace).
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      # pyproject's `license-files` makes hatchling require this at build time.
      ../LICENSE
      ../src
      ../tests
    ];
  };

  build-system = [ python3Packages.hatchling ];
  dependencies = [ python3Packages.fastmcp ];

  nativeBuildInputs = [ makeWrapper ];

  nativeCheckInputs = [
    python3Packages.pytestCheckHook
    python3Packages.pytest-asyncio
    git
  ];

  # The suite is fully mocked at the bd/subprocess seam, but bd.py resolves a
  # workspace at import time. Point it somewhere disposable so a build can never
  # touch (or depend on) a real beads database.
  preCheck = ''
    export MAILBOX_WORKSPACE="$(mktemp -d)"
    export HOME="$(mktemp -d)"
  '';

  pythonImportsCheck = [ "claude_mailbox" ];

  # `bd` is deliberately a PATH *suffix*, not a prefix: mailbox state lives in a
  # shared machine-wide beads database that carries schema migrations, so
  # forcing this closure's beads ahead of a user's own (often newer) `bd` risks
  # schema skew against a live database. A user with bd installed keeps using
  # theirs; this one is only the fallback. Flip preferSystemBd to false to pin.
  postInstall =
    let
      runtimeBins = lib.makeBinPath [
        beads
        git
      ];
      mode = if preferSystemBd then "--suffix" else "--prefix";
    in
    ''
      for bin in claude-mailbox mailbox; do
        wrapProgram $out/bin/$bin ${mode} PATH : ${runtimeBins}
      done
    '';

  meta = {
    description = "MCP mailbox for cross-talk between concurrent Claude Code sessions";
    homepage = "https://github.com/zach-source/claude-mailbox";
    license = lib.licenses.mit;
    mainProgram = "claude-mailbox";
    platforms = lib.platforms.unix;
  };
}
