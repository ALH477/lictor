{
  description = "lictor — an autolith-style terminal agent for Exsecutor";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # trvthnvke (README claim gate) and checks.readme land in a later wave,
    # once lictor has a README with claims worth gating. See CLAUDE.md-style
    # gate used by Exsecutor for the pattern this will follow.
  };

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      python = pkgs.python3;
      lib = pkgs.lib;

      # Pin the Agent SDK ahead of the nixpkgs-packaged 0.2.153: 0.2.160 fixes
      # "Stream closed" on a follow-up turn after a subagent finishes while
      # SDK MCP servers / can_use_tool / hooks are in use, which is exactly
      # lictor's configuration (M2+). nixpkgs' derivation also omits
      # jsonschema as a direct dependency (it works today only because it
      # comes in transitively via `mcp`); add it explicitly here.
      sdk = python.pkgs.claude-agent-sdk.overridePythonAttrs (old: rec {
        version = "0.2.163";
        src = pkgs.fetchFromGitHub {
          owner = "anthropics";
          repo = "claude-agent-sdk-python";
          tag = "v${version}";
          hash = "sha256-QG6y0/VGhyqBMiBarGfHxwF61qieDhzveQtA3cNWPok=";
        };
        dependencies = old.dependencies ++ [ python.pkgs.jsonschema ];

        # tests/test_run_end_subprocess.py spawns a stand-in CLI through a
        # `#!/usr/bin/env python3` shebang script (chmod 0o755, executed
        # directly). The nix build sandbox has no /usr/bin/env (no FHS), so
        # the exec fails with ENOENT and the SDK reports CLINotFoundError —
        # an artifact of the sandbox, not a defect in the SDK; the upstream
        # test module already skips this scenario on win32 for the same
        # shebang-portability reason. Disabled here only, not for lictor's
        # own test suite.
        disabledTestPaths = (old.disabledTestPaths or []) ++ [
          "tests/test_run_end_subprocess.py"
        ];
      });

      lictor = python.pkgs.buildPythonApplication {
        pname = "lictor";
        version = "0.1.0";
        pyproject = true;
        src = ./.;

        build-system = [ python.pkgs.hatchling ];
        dependencies = [ sdk ];

        nativeCheckInputs = [
          python.pkgs.pytestCheckHook
          python.pkgs.pytest-asyncio
        ];

        makeWrapperArgs = [
          "--prefix PATH : ${lib.makeBinPath [ pkgs.git pkgs.bubblewrap ]}"
        ];
      };
    in
    {
      packages.${system}.default = lictor;

      apps.${system}.default = {
        type = "app";
        program = "${lictor}/bin/lictor";
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = [
          (python.withPackages (ps: [ sdk ps.pytest ps.pytest-asyncio ps.ruff ]))
          pkgs.git
          pkgs.bubblewrap
        ];
      };
    };
}
