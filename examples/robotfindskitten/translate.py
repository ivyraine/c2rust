import json
import os
import shlex
import sys
from plumbum.cmd import mv, mkdir, rename, sed, rustc, cargo, rm
from plumbum import local

# Path to the root of the robotfindskitten codebase
RFK_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'repo'))

sys.path.append(os.path.join(RFK_DIR, '../../../scripts'))
from common import *
import transpile


# List of rust-refactor commands to run.

REFACTORINGS = [
        'wrapping_arith_to_normal',
        'struct_assign_to_update',
        'struct_merge_updates',

        '''
            select target 'item(messages); mark(array); desc(match_ty(*mut __t));' ;
            rewrite_expr '__e as marked!(*mut __t)' '__e' ;
            rewrite_ty 'marked!(*mut __t)' '*const __t' ;
            rewrite_expr 'def!(messages, array)[__e]'
                'def!(messages, array)[__e] as *mut libc::c_char'
        ''',

        # We can't make these immutable until we remove all raw pointers from
        # their types.  *const and *mut are not `Sync`, which is required for
        # all immutable statics.  (Presumably anything goes for mutable
        # statics, since they're unsafe to access anyway.)
        #'''
        #    select target 'crate; child(static && name("ver|messages"));' ;
        #    set_mutability imm
        #''',

        '''
            select target 'crate; child(static && mut);' ;
            static_collect_to_struct State S
        ''',
        '''
            select target 'crate; desc(fn && !name("main"));' ;
            set_visibility ''
        ''',
        '''
            select target 'crate; child(static && name("S"));' ;
            select user 'crate; desc(fn && !name("main"));' mark='user' ;
            static_to_local_ref
        ''',

        '''
            select target 'crate; desc(foreign_mod);' ;
            create_item 'mod ncurses {}' after
        ''',


        r'''
            select target 'crate; desc(mod && name("ncurses"));' ;
            create_item '
                macro_rules! printw {
                    ($($args:tt)*) => {
                        ::printw(b"%s\0" as *const u8 as *const libc::c_char,
                                 ::std::ffi::CString::new(format!($($args)*))
                                    .unwrap().as_ptr())
                    };
                }
            ' after
        ''',
        '''
            select printw 'item(printw);' ;

            copy_marks printw fmt_arg ;
            mark_arg_uses 0 fmt_arg ;

            select fmt_str 'marked(fmt_arg); desc(expr && !match_expr(__e as __t));' ;

            copy_marks printw calls ;
            mark_callers calls ;

            rename_marks fmt_arg target ;
            convert_format_string ;
            delete_marks target ;

            rename_marks calls target ;
            func_to_macro printw ;
        ''',


        r'''
            select target 'crate; desc(item && name("printw"));' ;
            create_item '
                macro_rules! mvprintw {
                    ($y:expr, $x:expr, $($args:tt)*) => {
                        ::mvprintw($y, $x, b"%s\0" as *const u8 as *const libc::c_char,
                                 ::std::ffi::CString::new(format!($($args)*))
                                    .unwrap().as_ptr())
                    };
                }
            ' after
        ''',
        '''
            select mvprintw 'item(mvprintw);' ;

            copy_marks mvprintw fmt_arg ;
            mark_arg_uses 2 fmt_arg ;

            select fmt_str 'marked(fmt_arg); desc(expr && !match_expr(__e as __t));' ;

            copy_marks mvprintw calls ;
            mark_callers calls ;

            rename_marks fmt_arg target ;
            convert_format_string ;
            delete_marks target ;

            rename_marks calls target ;
            func_to_macro mvprintw ;
        ''',

        '''
            select printf 'item(printf);' ;

            copy_marks printf fmt_arg ;
            mark_arg_uses 0 fmt_arg ;

            select fmt_str 'marked(fmt_arg); desc(expr && !match_expr(__e as __t));' ;

            copy_marks printf calls ;
            mark_callers calls ;

            rename_marks fmt_arg target ;
            convert_format_string ;
            delete_marks target ;

            rename_marks calls target ;
            func_to_macro print ;
        ''',


        # retype ver + messages

        # Change type of `ver`
        '''
            select target 'item(ver); mark(parent); child(match_ty(*mut libc::c_char));' ;
            rewrite_ty 'marked!(*mut libc::c_char)' "&'static str" ;
            delete_marks target ;
        '''
        # Remove casts from `ver` initializer
        '''
            select target 'marked(parent); desc(match_expr(__e as __t));' ;
            rewrite_expr 'marked!(__e as __t)' '__e' ;
            delete_marks target ;
        '''
        # Convert `ver` initializer from b"..." to "...".
        # Note we can't remove the null terminator yet because we're still
        # using CStr when doing the actual printing.
        '''
            select target 'marked(parent); child(expr);' ;
            bytestr_to_str ;
            delete_marks target ;
        '''
        # Fix up uses of `ver`
        '''
            type_fix_rules '*, &str, *const __t => __old.as_ptr()' ;
        ''',

        '''
            select target 'item(messages); mark(parent);
                child(ty); desc(match_ty(*const libc::c_char));' ;
            rewrite_ty 'marked!(*const libc::c_char)' "&'static str" ;
            delete_marks target ;
            select target 'marked(parent); desc(match_expr(__e as __t));' ;
            rewrite_expr 'marked!(__e as __t)' '__e' ;
            delete_marks target ;
            select target 'marked(parent); desc(expr);' ;
            bytestr_to_str ;
            delete_marks target ;
            type_fix_rules
                '*, &str, *const __t => __old.as_ptr()'
                '*, &str, *mut __t => __old.as_ptr() as *mut __t' ;
        ''',
]




idiomize = get_cmd_or_die(config.RREF_BIN)

def run_idiomize(args, mode='inplace'):
    full_args = ['-r', mode] + args + [
            '--', 'src/robotfindskitten.rs', '--crate-type=dylib',
            '-L{rust_libdir}/rustlib/{triple}/lib/'.format(
                rust_libdir=get_rust_toolchain_libpath(),
                triple=get_host_triplet())]

    ld_lib_path = get_rust_toolchain_libpath()

    # don't overwrite existing ld lib path if any...
    if 'LD_LIBRARY_PATH' in local.env:
        ld_lib_path += ':' + local.env['LD_LIBRARY_PATH']

    with local.env(RUST_BACKTRACE='1',
                   LD_LIBRARY_PATH=ld_lib_path):
        with local.cwd(os.path.join(RFK_DIR, 'rust')):
            idiomize[full_args]()


def main():
    os.chdir(RFK_DIR)
    print('in %s' % RFK_DIR)


    # Remove object files that will confuse `transpile`
    rm['-f', 'src/robotfindskitten.o']()


    # Actually translate
    with open('compile_commands.json', 'r') as f:
        transpile.transpile_files(f,
                emit_build_files=False,
                verbose=True)


    # Move rust files into rust/src
    mkdir['-vp', 'rust/src']()
    mv['-v', local.path('src') // '*.rs', 'rust/src/']()


    # Refactor
    for refactor_str in REFACTORINGS:
        refactor_args = shlex.split(refactor_str)
        print('REFACTOR: %r' % (refactor_args,))
        run_idiomize(refactor_args)


if __name__ == '__main__':
    main()
