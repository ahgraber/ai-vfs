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
          # Compatible fallback: Colima (Lima VM running dockerd). If you already
          # run Colima, the shellHook below auto-detects its docker socket; just:
          #     colima start
          #     docker compose -f tests/integration/docker-compose.yaml up -d
          #   Note: Colima's docker runtime requires the docker CLI client on PATH.
          #   pkgs.docker on darwin is client-only (clientOnly = true by default on
          #   non-Linux), so it is safe to include here — no daemon is pulled in.
          #
          # The shellHook resolves DOCKER_HOST/CONTAINER_HOST in this order:
          #   1. an already-running `podman machine` socket  (primary)
          #   2. a running Colima docker socket               (fallback)
          #   3. otherwise leaves the environment untouched   (default Docker /
          #      Linux native socket — never override on non-darwin).
          #
          # One-command path: scripts/integration-tests.sh handles engine
          # resolution, stack up/down, MinIO bucket creation, env exports, and
          # pytest invocation automatically (see scripts/integration-tests.sh --help).
          containerPackages =
            [
              pkgs.docker-compose # primary compose provider for `podman compose`
              pkgs.podman-compose # Docker-free compose fallback
            ]
            ++ pkgs.lib.optionals pkgs.stdenv.isDarwin [
              pkgs.podman # bundles vfkit + gvproxy on darwin
              pkgs.colima # compatible fallback VM (dockerd in Lima)
              pkgs.qemu # colima's default VM backend
              pkgs.docker # CLI client only on darwin (colima's docker runtime needs it)
            ];

          # Darwin-only socket resolution. Guarded so Linux users of this flake
          # (native daemon socket) are never affected.
          containerShellHook =
            pkgs.lib.optionalString pkgs.stdenv.isDarwin ''
              # Prefer Podman's compose provider (reference implementation) but
              # respect an explicit override.
              export PODMAN_COMPOSE_PROVIDER="''${PODMAN_COMPOSE_PROVIDER:-docker-compose}"

              if [ -z "''${DOCKER_HOST:-}" ] && [ -z "''${CONTAINER_HOST:-}" ]; then
                _aivfs_colima_sock="$HOME/.colima/default/docker.sock"
                # Discover the podman machine socket dynamically; the path lives
                # under $TMPDIR (e.g. /var/folders/.../T/podman/...) and varies
                # across podman versions and machines — do not hard-code it.
                _aivfs_podman_sock=""
                if command -v podman >/dev/null 2>&1; then
                  _aivfs_podman_sock="$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null || true)"
                fi
                if [ -n "$_aivfs_podman_sock" ] && [ -S "$_aivfs_podman_sock" ]; then
                  export DOCKER_HOST="unix://$_aivfs_podman_sock"
                  export CONTAINER_HOST="unix://$_aivfs_podman_sock"
                  echo "aivfs devshell: using podman machine socket ($DOCKER_HOST)"
                elif [ -S "$_aivfs_colima_sock" ]; then
                  export DOCKER_HOST="unix://$_aivfs_colima_sock"
                  echo "aivfs devshell: using colima docker socket ($DOCKER_HOST)"
                else
                  echo "aivfs devshell: no container VM detected; run 'podman machine start' (or 'colima start') for integration tests"
                fi
                unset _aivfs_podman_sock _aivfs_colima_sock
              fi
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
          ++ containerPackages;

          # imports = [ (pkgs.devshell.importTOML ./devshell.toml) ];
          shellHook = containerShellHook;
        };
    });
}
