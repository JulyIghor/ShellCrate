# Build the bundled tools

Use the source edition identified by **Help > Source for This Build** in your installed copy of ShellCrate. Download the repository at that tag and the accompanying tar files from its release page. Each tar file holds the original archives, patches, or build inputs named in the source index. The archives are grouped so a single shared dependency only needs one copy.

## Verify the download

Python 3.9 or newer is sufficient for the verifier. Point it at the directory holding the release assets. To retain the original inputs as ordinary files, supply a new directory with `--extract`.

```sh
python3 verify_sources.py --assets ~/Downloads/shellcrate-source-assets
python3 verify_sources.py --assets ~/Downloads/shellcrate-source-assets --extract ./materials
```

The verifier checks the accompanying recipes and legal notices, each archive, and each archived file against its SHA-256 digest. It first validates names and file types and writes only regular files during extraction. Keep the source index from the same edition as the assets. [Source index](distribution/source-index.json)

The source editions also include Cargo crates and pinned Git sources, Go modules and runtime sources, and Cabal packages and GHC runtime sources used by the Haskell tools. The Cabal inventory identifies the package versions found in Pandoc and ShellCheck. [Cabal package inventory](distribution/build/Tools/haskell-sources.json)

## Midnight Commander and zsh

Use an Apple silicon Mac with the Apple SDK and command-line build tools. Midnight Commander also needs CMake, Ninja, pkg-config, and gettext's build tools available on PATH. The exact package versions and build options are in the recipes. The Python builder accepts separate, empty working directories without spaces. It applies the two dated MC patches in the order recorded in the lock. It builds zsh from its upstream source.

```sh
python3 rebuild.py mc --sources ./materials --work /tmp/shellcrate-build-mc
python3 rebuild.py zsh --sources ./materials --work /tmp/shellcrate-build-zsh
```

The last line gives the resulting executable. Build logs are in the work directory under `.build/logs`. For just one of these builds, the verifier can limit extraction to its inputs.

```sh
python3 verify_sources.py --assets ~/Downloads/shellcrate-source-assets --for-build mc --extract ./mc-materials
```

To change a tool or a library, unpack its upstream source, edit your copy, and pass it to the builder. MC's original link inputs include two static archives and the libssh2 headers. You can replace them with libraries you build yourself. Each prefix uses the conventional `include` and `lib` directories. The same process runs independently of the installed app.

```sh
python3 rebuild.py mc --sources ./materials --work /tmp/shellcrate-build-mc \
  --replace-source glib=/path/to/modified-glib \
  --ssh-prefix /path/to/libssh2-prefix --crypto-prefix /path/to/openssl-prefix
```

The GLib, PCRE2, S-Lang, gettext compatibility library, Meson, and GVDB inputs used by this build are included. The source index records the OpenSSL and libssh2 source and patch versions alongside the link inputs. [Build recipes](distribution/build/script) · [MC patches](distribution/build/Tools/mc/patches)

## Homebrew-built tools

Find the command in the catalog and open its recipe. Each Ruby file is the formula recovered from the actual bottle used for this edition. Its source, configuration options, resources, and patch order describe Homebrew's build. Tap-local patch files are present under their original paths. External patches are also provided as readable files. The private libraries listed for each command identify the components copied into its package. [Catalog](distribution/CATALOG.md) · [Per-command components](distribution/packages.json) · [Homebrew formulas and patches](distribution/homebrew)

To build a formula using Homebrew, create a local tap and copy this edition's formulas and patches into it. For example, these commands build the 7-Zip formula from source. Homebrew resolves dependencies for your own machine and can access upstream servers. For a historical dependency, use the matching recipe and source recorded in this edition. Upstream build systems can also be run directly using the options from the formula.

```sh
brew tap-new local/shellcrate
tap_dir="$(brew --repository local/shellcrate)"
mkdir -p "$tap_dir/Formula" "$tap_dir/Patches"
cp -R distribution/homebrew/Formula/. "$tap_dir/Formula/"
cp -R distribution/homebrew/Patches/. "$tap_dir/Patches/"
brew install --build-from-source local/shellcrate/sevenzip
```

ShellCrate adjusts Mach-O library load paths to find the private libraries shipped with a tool. The packaging code and version lock record that step. Source builds can differ in compiler metadata, build paths and timestamps. To rebuild a tool for use outside ShellCrate, use its normal upstream installation instructions. [Packaging code](distribution/build/script/homebrew_tools.py) · [Version lock](distribution/build/Tools/index.json)
