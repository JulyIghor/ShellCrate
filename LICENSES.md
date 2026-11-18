# Licenses and copyright notices

ShellCrate packages independently licensed command-line programs and libraries. The catalog identifies the version and the components shipped with each provider. This includes helpers used for SSH and HTTPS. In the app, open a tool's **Licenses** tab to read the notices offline. **Help > Source for This Build** opens the corresponding source edition. [Per-provider inventory](distribution/packages.json) and [complete bundled notices](distribution/licenses/ThirdPartyNotices.txt)

## Programs, libraries, and patches

Each source archive contains its upstream licensing terms. The accompanying notice index lists the byte ranges for each component in the app's notices file. Some programs use different licenses for individual files or linked libraries. The identifier shown next to a command summarizes its primary license. [Notice index](distribution/licenses/ToolLicenses.json)

The source editions retain the original documentation sources. The 7-Zip manual comes from the matching macOS release and is included as editable HTML with its upstream license. [7-Zip manual sources](distribution/manuals/7zz)

- Midnight Commander carries the GNU GPL. The two ShellCrate patches include the dates and descriptions of the changes. The source edition includes the library sources, build recipes, and the inputs needed to relink the executable with modified libraries. [Patches](distribution/build/Tools/mc/patches) and [rebuild instructions](BUILDING.md)
- The included FFmpeg recipe enables GPL and version 3 features. 7-Zip includes LGPL code and components with additional terms, including the unRAR restriction. Read the notices for the version you received. [FFmpeg licensing](https://ffmpeg.org/legal.html) and [7-Zip licensing](https://www.7-zip.org/license.txt)
- The Mozilla-derived certificate bundle ships as editable PEM data. The source edition includes the distributed PEM, upstream input, and Homebrew's recipe. [Distributed certificate data](distribution/data/cacert.pem) and [Mozilla Public License 2.0](https://www.mozilla.org/en-US/MPL/2.0/)
- libvterm retains its MIT license. Homebrew's formula collection retains its BSD 2-Clause license. Files included by an upstream patch or formula can carry their own copyright notices and terms. [libvterm license](distribution/licenses/libvterm.txt) and [Homebrew license](distribution/homebrew/LICENSE.txt)

## ShellCrate build utilities

Ihor July makes the original ShellCrate code in `rebuild.py`, `verify_sources.py`, and `distribution/build/script/` available under the MIT license. Changes to upstream tool sources are distributed under the applicable upstream terms. The ShellCrate application has separate terms. Commands marked **System** in the app use the macOS installation. [Build tools license](BUILD-TOOLS-LICENSE.txt) and [Apple Open Source](https://opensource.apple.com/)
