{
  description = "lictor — an autolith-style terminal agent for Exsecutor";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # The README claim gate. Every claim README.md makes about this repo is
    # checked by this tool, so a sentence cannot outlive the behaviour it
    # describes. Same gate the Exsecutor tree uses.
    trvthnvke = {
      url = "github:ALH477/TrvthNvke";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, trvthnvke }:
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
          # tests/test_overlay.py commits to the private mutation repository,
          # so the check phase needs git even though the runtime wrapper
          # already puts it on PATH. Without it the package stopped building
          # the moment M3's overlay tests landed.
          pkgs.git
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
          trvthnvke.packages.${system}.default
        ];
      };

      # The README gate, as a flake check: `nix flake check` fails if a
      # claim in README.md no longer holds. Mirrors Exsecutor's own
      # checks.readme, and is equivalent to `trvthnvke verify --fail`.
      checks.${system} = {
        readme = pkgs.runCommand "check-trvthnvke-readme" {
          nativeBuildInputs = [ trvthnvke.packages.${system}.default ];
          src = self;
        } ''
          cp -r "$src"/. .
          chmod -R u+w .
          trvthnvke verify --fail
          mkdir -p "$out"
          echo ok > "$out/receipt"
        '';
        tests = lictor;
      };

    };
}
