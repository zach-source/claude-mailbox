# Homebrew formula for a personal tap:
#
#   brew tap zach-source/claude-mailbox https://github.com/zach-source/claude-mailbox
#   brew install zach-source/claude-mailbox/claude-mailbox
#
# Not homebrew-core quality and not trying to be: core requires every Python
# dependency vendored as a `resource` block for a fully offline, pinned install.
# fastmcp's runtime closure is ~69 packages including Rust-built `cryptography`
# and `pydantic-core`, which would mean ~60 hand-maintained resource stanzas
# regenerated on every fastmcp bump. This formula resolves them from PyPI at
# install time instead — simpler and honest for a tap. Run
# `brew update-python-resources Formula/claude-mailbox.rb` if this is ever
# submitted upstream.
class ClaudeMailbox < Formula
  include Language::Python::Virtualenv

  desc "MCP mailbox for cross-talk between concurrent Claude Code sessions"
  homepage "https://github.com/zach-source/claude-mailbox"
  # Must be >= v0.6.0: earlier tags resolve the beads workspace to
  # <prefix>/lib/pythonX.Y (see bd.py _default_workspace), which no installed
  # build can use. Refresh both lines on every release:
  #   curl -sL <url> | shasum -a 256
  url "https://github.com/zach-source/claude-mailbox/archive/refs/tags/v0.7.0.tar.gz"
  sha256 "049b5882d02bd1a0f757524407313116d8b2f7e382118e428a390cb7149f6cc8"
  license "MIT"
  head "https://github.com/zach-source/claude-mailbox.git", branch: "main"

  # The server shells out to `bd` for all state, and to `git` to detect the
  # session's project/branch (git comes from the system on macOS).
  depends_on "beads"
  depends_on "python@3.13"

  def install
    venv = virtualenv_create(libexec, "python3.13")
    venv.pip_install_and_link buildpath
  end

  def caveats
    <<~EOS
      claude-mailbox keeps its beads workspace per-user, at
        #{ENV.fetch("XDG_DATA_HOME", "~/.local/share")}/claude-mailbox
      (override with MAILBOX_WORKSPACE). Initialize it once:

        mkdir -p ~/.local/share/claude-mailbox
        bd init -C ~/.local/share/claude-mailbox
        bd init --global    # creates the machine-wide beads_global database

      Then wire it into Claude Code / codex:

        "mailbox": { "command": "#{opt_bin}/claude-mailbox" }
    EOS
  end

  test do
    # `mailbox who` exercises the real bd path and needs an initialized
    # workspace, so assert on the packaging instead: both entry points import
    # and expose their CLI without a database present.
    ENV["MAILBOX_WORKSPACE"] = testpath/"ws"
    script = "import claude_mailbox, claude_mailbox.server as s; print(s.mcp.name)"
    assert_match "claude-mailbox",
                 shell_output("#{libexec}/bin/python -c '#{script}'")
    assert_predicate bin/"claude-mailbox", :executable?
    assert_predicate bin/"mailbox", :executable?
  end
end
