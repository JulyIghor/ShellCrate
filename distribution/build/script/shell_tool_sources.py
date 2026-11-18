"""Normal upstream build options for the bundled command interpreters."""
import shutil


def build_shell_source(recipe, source, prefix, env, jobs, log, run):
    if recipe == 'bash':
        # Compile out loadable builtins. Ordinary child execution still inherits
        # the invocation sandbox. Readline is built from the upstream source.
        env.update(ac_cv_func_dlopen='no', ac_cv_func_dlclose='no',
                   ac_cv_func_dlsym='no', ac_cv_lib_dl_dlopen='no')
        run(['./configure', '--prefix=' + str(prefix), '--disable-nls', '--without-bash-malloc'], source, env, log)
        run(['/usr/bin/make', '-j' + jobs, 'bash'], source, env, log)
        return ['source/bash']
    run(['./configure', '--prefix=' + str(prefix), '--disable-dynamic', '--disable-pcre',
         '--disable-gdbm', '--disable-cap', '--enable-multibyte', '--enable-etcdir=/nonexistent',
         '--enable-fndir=/nonexistent', '--enable-site-fndir=/nonexistent'], source, env, log)
    run(['/usr/bin/make', 'config.h', 'config.modules'], source, env, log)
    run(['/usr/bin/make', '-C', 'Src', '-j' + jobs, 'all'], source, env, log)
    # The upstream installer keeps function grouping and expands its supported
    # module configuration. Override the installation destination, not sources.
    run(['/usr/bin/make', 'install.fns', 'fndir=' + str(prefix / 'share/zsh/functions'),
         'sitefndir=no', 'FUNCTIONS_SUBDIRS=no'], source, env, log)
    binary = prefix / 'bin/zsh'
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / 'Src/zsh', binary)
    return ['prefix/bin/zsh', *[str(p.relative_to(prefix.parent))
            for p in sorted((prefix / 'share/zsh').rglob('*')) if p.is_file()]]
