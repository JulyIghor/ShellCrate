# ShellCrate

ShellCrate is a native macOS app for learning command-line tools and using them from your own terminal. Try a command on practice files in the Playground, read its manual, then enable it in a terminal workspace. Each workspace can give the same tool a different version, HOME folder, set of folder permissions, and network profile.

The main app runs in App Sandbox without a network entitlement. ShellCrate starts each managed tool invocation in a separate process and applies the access configured for that call. The Gallery includes tools shipped with ShellCrate and selected commands supplied by macOS. [ShellCrate website](https://shellcrate.app)

## Get the source for your copy

In ShellCrate, choose **Help > Source for This Build**, or open a bundled tool's **Licenses** tab. The link identifies the source edition for that installation. Download the repository source and the accompanying archive files from that same release. You can get the sources without purchasing ShellCrate or signing in. [Source editions](https://github.com/julyighor/ShellCrate/releases)

The catalog lists each command, version, provider, and build recipe. The source index records the original archive names, checksums, patches, and dependency versions. The notices include the copyright and license text used by the app. Commands supplied by macOS are maintained by Apple. [Bundled commands](distribution/CATALOG.md) · [Source index](distribution/source-index.json) · [License notices](distribution/licenses/ThirdPartyNotices.txt) · [Apple Open Source](https://opensource.apple.com/)

## Build or modify a tool

The source editions include the upstream sources, the Homebrew recipes carried in the distributed bottles, and ShellCrate's build scripts for Midnight Commander and zsh. Both dated Midnight Commander changes are available as plain diffs. You can build these programs independently of the ShellCrate app. [Build instructions](BUILDING.md) · [Midnight Commander patches](distribution/build/Tools/mc/patches)

The covered components and ShellCrate's changes to them remain available under their respective licenses. The repository also contains the MIT-licensed libvterm terminal library. ShellCrate uses the 7-Zip command, licensed primarily under the GNU LGPL with additional file-specific terms including the unRAR restriction. The included FFmpeg build enables GPL and version 3 features. The full upstream notices describe these terms. [Licensing details](LICENSES.md) · [7-Zip](https://www.7-zip.org/) · [FFmpeg](https://ffmpeg.org/)

To request a tool or report a missing source file, open an issue with the command name and the source edition shown by your copy of ShellCrate. [ShellCrate issues](https://github.com/julyighor/ShellCrate/issues)
