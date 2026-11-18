"""Private static dependencies for the bundled terminal file manager."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / '.build/source-build'


def build_terminal_source(recipe, directory, source, prefix, env, jobs, log, run):
    if recipe in {'meson', 'gvdb', 'proxy-libintl'}:
        return [str(path.relative_to(directory)) for path in sorted(source.rglob('*')) if path.is_file()]
    if recipe == 'slang':
        run(['./configure', '--prefix=' + str(prefix), '--without-x', '--without-pcre',
             '--without-onig', '--without-png'], source, env, log)
        # Build only the static library. No interpreter or loadable modules ship.
        run(['/usr/bin/make', '-C', 'src', 'static'], source, env, log)
        (prefix / 'lib/pkgconfig').mkdir(parents=True)
        (prefix / 'include').mkdir()
        shutil.copy2(source / 'src/objs/libslang.a', prefix / 'lib')
        shutil.copy2(source / 'src/slang.h', prefix / 'include')
        (prefix / 'lib/pkgconfig/slang.pc').write_text(
            'prefix=' + str(prefix) + '\nName: slang\nDescription: S-Lang terminal library\n'
            'Version: 2.3.3\nLibs: -L${prefix}/lib -lslang -lm\nCflags: -I${prefix}/include\n')
        return [str(path.relative_to(directory)) for path in sorted(prefix.rglob('*')) if path.is_file()]
    if recipe == 'pcre2':
        build = directory / 'cmake'
        run(['cmake', '-S', source, '-B', build, '-DCMAKE_BUILD_TYPE=MinSizeRel',
             '-DCMAKE_OSX_ARCHITECTURES=arm64', '-DCMAKE_OSX_DEPLOYMENT_TARGET=' + env['MACOSX_DEPLOYMENT_TARGET'],
             '-DCMAKE_OSX_SYSROOT=' + env['SDKROOT'], '-DCMAKE_INSTALL_PREFIX=' + str(prefix),
             '-DCMAKE_INSTALL_LIBDIR=lib', '-DBUILD_SHARED_LIBS=OFF', '-DPCRE2_BUILD_TESTS=OFF',
             '-DPCRE2_BUILD_PCRE2GREP=OFF', '-DPCRE2_SUPPORT_JIT=OFF'], source, env, log)
        run(['cmake', '--build', build, '--parallel', jobs], source, env, log)
        run(['cmake', '--install', build], source, env, log)
        return ['prefix/lib/libpcre2-8.a', 'prefix/include/pcre2.h', 'prefix/lib/pkgconfig/libpcre2-8.pc']
    if recipe == 'glib':
        pcre = BUILD / 'pcre2/prefix'
        build = directory / 'meson'
        meson = [sys.executable, str(BUILD / 'meson/source/meson.py')]
        shutil.copytree(BUILD / 'gvdb/source', source / 'subprojects/gvdb', dirs_exist_ok=True)
        shutil.copytree(BUILD / 'proxy-libintl/source', source / 'subprojects/proxy-libintl', dirs_exist_ok=True)
        # GObject is configured by upstream Meson but is not linked or shipped.
        # Its configure-time libffi dependency comes from the macOS SDK.
        system_pc = directory / 'sdk-pkgconfig'
        system_pc.mkdir()
        (system_pc / 'libffi.pc').write_text(
            'Name: libffi\nDescription: macOS SDK libffi\nVersion: 3.4.4\n'
            'Libs: -lffi\nCflags: -I' + env['SDKROOT'] + '/usr/include/ffi\n')
        env['PKG_CONFIG_LIBDIR'] = str(pcre / 'lib/pkgconfig') + ':' + str(system_pc)
        run(meson + ['setup', build, source, '--prefix=' + str(prefix), '--libdir=lib',
             '--buildtype=minsize', '--default-library=static', '--wrap-mode=nodownload',
             '-Dtests=false', '-Dinstalled_tests=false', '-Dnls=disabled', '-Dintrospection=disabled',
             '-Ddocumentation=false', '-Dman-pages=disabled', '-Dsysprof=disabled', '-Dlibmount=disabled',
             '-Dselinux=disabled', '-Ddtrace=disabled', '-Dsystemtap=disabled'], source, env, log)
        run(['ninja', '-C', build, '-j', jobs, 'glib/libglib-2.0.a', 'gmodule/libgmodule-2.0.a',
             'subprojects/proxy-libintl/libintl.a'], source, env, log)
        (prefix / 'lib/pkgconfig').mkdir(parents=True)
        (prefix / 'lib/glib-2.0/include').mkdir(parents=True)
        (prefix / 'include/glib-2.0/glib').mkdir(parents=True)
        for name in ['glib', 'gmodule']:
            shutil.copy2(build / name / ('lib' + name + '-2.0.a'), prefix / 'lib')
        shutil.copy2(build / 'subprojects/proxy-libintl/libintl.a', prefix / 'lib')
        shutil.copy2(source / 'subprojects/proxy-libintl/libintl.h', prefix / 'include')
        for header in (source / 'glib').rglob('*.h'):
            target = prefix / 'include/glib-2.0/glib' / header.relative_to(source / 'glib')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(header, target)
        for header in ['glib.h', 'glib-unix.h']:
            shutil.copy2(source / 'glib' / header, prefix / 'include/glib-2.0')
        shutil.copy2(source / 'gmodule/gmodule.h', prefix / 'include/glib-2.0')
        shutil.copy2(build / 'glib/glibconfig.h', prefix / 'lib/glib-2.0/include')
        for header in ['gversionmacros.h', 'glib-visibility.h']:
            shutil.copy2(build / 'glib' / header, prefix / 'include/glib-2.0/glib')
        for name in ['glib-2.0', 'gmodule-2.0', 'gmodule-no-export-2.0']:
            shutil.copy2(build / 'meson-private' / (name + '.pc'), prefix / 'lib/pkgconfig')
        return [str(path.relative_to(directory)) for path in sorted(prefix.rglob('*')) if path.is_file()]
    if recipe != 'mc':
        raise ValueError('Unsupported terminal package recipe')
    glib = BUILD / 'glib/prefix'
    pcre = BUILD / 'pcre2/prefix'
    slang = BUILD / 'slang/prefix'
    env['PKG_CONFIG_LIBDIR'] = ':'.join(str(path / 'lib/pkgconfig') for path in [glib, pcre, slang])
    env['GLIB_LIBS'] = subprocess.check_output(['pkg-config', '--static', '--libs', 'glib-2.0'], env=env, text=True).strip()
    env['GLIB_LIBDIR'] = str(glib / 'lib')
    # Only static GLib archives exist in the private prefix. Keep ordinary -l
    # linking so upstream libtool does not merge an external archive into libmc.
    run(['./configure', '--prefix=' + str(prefix), '--with-screen=slang',
         '--without-subshell', '--without-x', '--without-gpm-mouse', '--disable-nls', '--disable-aspell',
         '--enable-vfs-sftp', '--disable-vfs-shell', '--disable-vfs-extfs', '--disable-vfs-sfs',
         '--disable-vfs-undelfs', '--enable-vfs-ftp', '--without-diff-viewer'], source, env, log)
    run(['/usr/bin/make', '-j' + jobs], source, env, log)
    run(['/usr/bin/make', 'install'], source, env, log)
    # Only data used by the internal panels, help and editor is packaged.
    # External-command helpers are disabled and are never shipped as resources.
    data = prefix / 'share/mc'
    shutil.rmtree(data / 'examples')
    for path in (prefix / 'etc/mc').iterdir():
        if path.is_file() and (path.suffix == '.keymap' or path.name in
                {'mc.menu', 'mc.ext.ini', 'mc.charsets', 'mcedit.menu'}):
            shutil.copy2(path, data / path.name)
    # Ordinary application configuration, not source or binary patches. The
    # internal viewer must not depend on `file` or external conversion scripts.
    (data / 'mc.ini').write_text('[Midnight-Commander]\nuse_file_to_guess_type=false\n')
    (data / 'mc.ext.ini').write_text('[mc.ext.ini]\nVersion=4.0\n')
    for path in data.rglob('*'):
        if path.is_file():
            path.chmod(0o644)
    return ['prefix/bin/mc'] + [str(path.relative_to(directory)) for path in sorted((prefix / 'share/mc').rglob('*')) if path.is_file()]
