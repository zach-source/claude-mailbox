{
  description = "MCP mailbox for cross-talk between concurrent Claude Code sessions, backed by beads (bd)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems =
        f:
        nixpkgs.lib.genAttrs systems (
          system:
          f {
            inherit system;
            pkgs = nixpkgs.legacyPackages.${system};
          }
        );
    in
    {
      packages = forAllSystems (
        { pkgs, ... }:
        rec {
          claude-mailbox = pkgs.callPackage ./nix/package.nix { };
          default = claude-mailbox;
        }
      );

      apps = forAllSystems (
        { system, ... }:
        let
          pkg = self.packages.${system}.claude-mailbox;
        in
        rec {
          # `nix run github:zach-source/claude-mailbox` starts the MCP server on
          # stdio — the form an MCP client entry invokes.
          claude-mailbox = {
            type = "app";
            program = "${pkg}/bin/claude-mailbox";
          };
          # `nix run github:zach-source/claude-mailbox#mailbox -- who`
          mailbox = {
            type = "app";
            program = "${pkg}/bin/mailbox";
          };
          default = claude-mailbox;
        }
      );

      devShells = forAllSystems (
        { pkgs, ... }:
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.uv
              pkgs.python3
              pkgs.beads
              pkgs.git
            ];
          };
        }
      );

      formatter = forAllSystems ({ pkgs, ... }: pkgs.nixfmt-tree);
    };
}
