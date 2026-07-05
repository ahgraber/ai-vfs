{
  description = "A simple flake to install dependencies for ai-zk";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    devshell.url = "github:numtide/devshell";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, devshell, flake-utils, nixpkgs }:
    flake-utils.lib.eachDefaultSystem (system: {
      devShells.default =
        let
          pkgs = import nixpkgs {
            inherit system;
            # bring devshell attribute into the pkgs
            # overlays = [ devshell.overlays.default ];
          };
          nodeEnv = import ./nix/default.nix;

          # --- container integration tests (tests/integration/docker-compose.yaml) ---
          #
          # Primary path on macOS: Podman.
          #   nixpkgs wraps `podman` with `vfkit` (applehv VM) and `gvproxy`
          #   (VM networking), so `podman machine init/start` works out of the
          #   box on Apple Silicon without Homebrew. `podman compose` is a thin
          #   wrapper that delegates to a compose provider; we install
          #   docker-compose (Podman's own default-precedence provider and the
          #   Compose reference implementation, most reliable for this fixture's
          #   healthchecks) plus podman-compose as a Docker-free fallback.
          #
          #   One-time setup, then run the tests:
          #     podman machine init        # creates the applehv VM
          #     podman machine start       # boots it (exposes a Docker-compat socket)
          #     podman compose -f tests/integration/docker-compose.yaml up -d
          #     uv run pytest tests/integration -q
          #     podman compose -f tests/integration/docker-compose.yaml down -v
          #
          # Lazy bring-up (see containerShellHook below): the shellHook defines `podman` / `docker`
          # wrapper functions so the FIRST command that needs the daemon ensures the machine's
          # API socket is live — (re)starting the machine if the socket is missing. Nothing
          # touches the daemon merely by entering the shell. On macOS the socket lives under
          # /tmp, which the OS reaps after ~3 days (leaving the VM "running" but unreachable), so
          # the wrapper checks the socket itself, not just `podman machine` state. The docker CLI
          # client (pkgs.docker, client-only on darwin — no daemon pulled in) is wrapped the same
          # way, pointed at the podman socket.
          containerPackages = [
            pkgs.docker-compose # primary compose provider for `podman compose`
            pkgs.podman-compose # Docker-free compose fallback
          ];

          # Real engine binaries, referenced by absolute store path from the shims below so the
          # shims are the only `podman`/`docker` on the devshell PATH (no collision, and the shim
          # deterministically wins over a system install).
          podmanReal = "${pkgs.podman}/bin/podman"; # bundles vfkit + gvproxy on darwin
          dockerReal = "${pkgs.docker}/bin/docker"; # CLI client only on darwin (no daemon)

          # Lazy machine bring-up shared by both shims: ensure the podman machine's API socket is
          # live, (re)starting the machine when it is missing. macOS reaps the socket from /tmp
          # after ~3 days, leaving the VM "running" but unreachable, so we check the socket itself.
          # When no machine exists at all, offer to `init` one — but only interactively (init is
          # heavy: it downloads a VM image), and only when /dev/tty is reachable so non-interactive
          # callers (CI, pytest subprocesses, direnv eval) fall back to a hint instead of hanging.
          ensureMachineSnippet = ''
            _aivfs_ensure() {
              sock="$(${podmanReal} machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null || true)"
              if [ -n "$sock" ] && [ -S "$sock" ]; then return 0; fi
              state="$(${podmanReal} machine inspect --format '{{.State}}' 2>/dev/null || true)"
              if [ -z "$state" ]; then
                if ! { : < /dev/tty; } 2>/dev/null; then
                  echo "aivfs: no podman machine — run 'podman machine init && podman machine start' once" >&2
                  return 1
                fi
                printf 'aivfs: no podman machine. Initialize one now? This downloads a VM image. [Y/n] ' > /dev/tty
                read -r reply < /dev/tty || reply=n
                # Fail safe: only an affirmative reply (or bare Enter) triggers the heavy init.
                case "$reply" in
                  "" | [Yy]*) ;;
                  *)
                    echo "aivfs: skipped — run 'podman machine init && podman machine start' when ready" >&2
                    return 1 ;;
                esac
                echo "aivfs: initializing podman machine (this can take a few minutes)..." >&2
                ${podmanReal} machine init || { echo "aivfs: 'podman machine init' failed" >&2; return 1; }
                ${podmanReal} machine start >/dev/null 2>&1 || { echo "aivfs: 'podman machine start' failed" >&2; return 1; }
                return 0
              fi
              echo "aivfs: podman machine socket unavailable — (re)starting machine..." >&2
              [ "$state" = "running" ] && ${podmanReal} machine stop >/dev/null 2>&1
              ${podmanReal} machine start >/dev/null 2>&1 || { echo "aivfs: 'podman machine start' failed" >&2; return 1; }
            }
          '';

          # Darwin-only PATH shims: `podman` / `docker` ensure the machine on first use, then exec
          # the real binary. PATH shims (not shellHook functions) so the lazy bring-up works in any
          # login shell — direnv exports env vars to zsh, but not bash functions.
          containerShimPkgs = pkgs.lib.optionals pkgs.stdenv.isDarwin [
            (pkgs.writeShellScriptBin "podman" ''
              ${ensureMachineSnippet}
              # Pass machine management straight through (no ensure, no recursion).
              if [ "''${1:-}" = "machine" ]; then exec ${podmanReal} "$@"; fi
              _aivfs_ensure || exit 1
              exec ${podmanReal} "$@"
            '')
            (pkgs.writeShellScriptBin "docker" ''
              ${ensureMachineSnippet}
              _aivfs_ensure || exit 1
              sock="$(${podmanReal} machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null || true)"
              exec env DOCKER_HOST="unix://$sock" ${dockerReal} "$@"
            '')
          ];

          # Darwin-only: prefer Podman's compose provider. (Machine bring-up lives in the shims.)
          containerShellHook =
            pkgs.lib.optionalString pkgs.stdenv.isDarwin ''
              export PODMAN_COMPOSE_PROVIDER="''${PODMAN_COMPOSE_PROVIDER:-docker-compose}"
            '';
        in
        # pkgs.devshell.mkShell {
        pkgs.mkShell {
          name = "aizk-devshell";

          # buildInputs = [
          #   nodeEnv.nodejs
          #   nodeEnv.nodePackages."${nodeEnv.packageName}"
          # ];
          # buildPhase = ''
          #   ln -s ${nodeEnv.nodeDependencies}/lib/node_modules ./node_modules
          #   export PATH="${nodeEnv.nodeDependencies}/bin:$PATH"
          # '';

          # a list of packages to add to the shell environment
          packages = [
            #--- cli ---
            # pkgs.ungoogled-chromium # not available on mac
            pkgs.pandoc
            #--- node ---
            pkgs.deno
            # pkgs.node2nix # removed from nixpkgs (used only by the commented nodeEnv flow); use buildNpmPackage if needed
            # nodejs_20 # nodejs runtime v20 for v8 javascript
          ]
          #--- containers (see containerPackages above) ---
          ++ containerPackages
          #--- lazy podman/docker PATH shims (darwin) ---
          ++ containerShimPkgs;

          # imports = [ (pkgs.devshell.importTOML ./devshell.toml) ];
          shellHook = containerShellHook;
        };
    });
}
