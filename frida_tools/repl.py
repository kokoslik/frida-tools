# -*- coding: utf-8 -*-
from __future__ import unicode_literals, print_function

import os
import shlex
import signal
import string
import threading
import sys
import time


def main():
    import codecs
    import hashlib
    import json
    import os
    import platform
    import re
    try:
        from urllib.request import build_opener
    except:
        from urllib2 import build_opener

    from colorama import Fore, Style
    import frida
    from prompt_toolkit.shortcuts import prompt
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.completion import Completion, Completer
    from prompt_toolkit.lexers import PygmentsLexer
    from prompt_toolkit.styles import Style as PromptToolkitStyle
    from pygments.lexers.javascript import JavascriptLexer
    from pygments.token import Token
    from timeit import default_timer as timer

    from frida_tools.application import ConsoleApplication
    from frida_tools.cli_formatting import format_compiling, format_compiled, format_diagnostic
    from frida_tools import _repl_magic

    class REPLApplication(ConsoleApplication):
        def __init__(self):
            self._script = None
            self._ready = threading.Event()
            self._stopping = threading.Event()
            self._errors = 0
            self._completer = FridaCompleter(self)
            self._cli = None
            self._last_change_id = 0
            self._compilers = {}
            self._monitored_files = {}
            self._autoperform = False
            self._autoperform_option = False
            self._autoreload = True
            self._quiet_start = None

            super(REPLApplication, self).__init__(self._process_input, self._on_stop)

            if self._have_terminal and not self._plain_terminal:
                style = PromptToolkitStyle([
                    ("completion-menu", "bg:#3d3d3d #ef6456"),
                    ("completion-menu.completion.current", "bg:#ef6456 #3d3d3d"),
                ])
                history = FileHistory(self._get_or_create_history_file())
                self._cli = PromptSession(lexer=PygmentsLexer(JavascriptLexer),
                                          style=style,
                                          history=history,
                                          completer=self._completer,
                                          complete_in_thread=True,
                                          enable_open_in_editor=True,
                                          tempfile_suffix=".js")
                self._dumb_stdin_reader = None
            else:
                self._cli = None
                self._dumb_stdin_reader = DumbStdinReader(valid_until=self._stopping.is_set)

            if not self._have_terminal:
                self._rpc_complete_server = start_completion_thread(self)

        def _add_options(self, parser):
            parser.add_argument("-l", "--load", help="load SCRIPT", metavar="SCRIPT", dest="user_scripts", action="append", default=[])
            parser.add_argument("-P", "--parameters", help="parameters as JSON, same as Gadget", metavar="PARAMETERS_JSON", dest="user_parameters")
            parser.add_argument("-C", "--cmodule", help="load CMODULE", dest="user_cmodule")
            parser.add_argument("--toolchain", help="CModule toolchain to use when compiling from source code", choices=['any', 'internal', 'external'], default='any')
            parser.add_argument("-c", "--codeshare", help="load CODESHARE_URI", metavar="CODESHARE_URI", dest="codeshare_uri")
            parser.add_argument("-e", "--eval", help="evaluate CODE", metavar="CODE", action='append', dest="eval_items")
            parser.add_argument("-q", help="quiet mode (no prompt) and quit after -l and -e", action='store_true', dest="quiet", default=False)
            parser.add_argument("-t", "--timeout", help="seconds to wait before terminating in quiet mode", dest="timeout", default=0)
            parser.add_argument("--pause", help="leave main thread paused after spawning program", action='store_const', const='pause', dest="on_spawn_complete", default='resume')
            parser.add_argument("-o", "--output", help="output to log file", dest="logfile")
            parser.add_argument("--eternalize", help="eternalize the script before exit", action='store_true', dest="eternalize", default=False)
            parser.add_argument("--exit-on-error", help="exit with code 1 after encountering any exception in the SCRIPT", action='store_true', dest="exit_on_error", default=False)
            parser.add_argument("--auto-perform", help="wrap entered code with Java.perform", action='store_true', dest="autoperform", default=False)
            parser.add_argument("--auto-reload", help="Enable auto reload of provided scripts and c module (on by default, will be required in the future)", action='store_true', dest="autoreload", default=True)
            parser.add_argument("--no-auto-reload", help="Disable auto reload of provided scripts and c module", action='store_false', dest="autoreload", default=True)

        def _initialize(self, parser, options, args):
            self._user_scripts = list(map(os.path.abspath, options.user_scripts))
            for user_script in self._user_scripts:
                with codecs.open(user_script, 'rb', 'utf-8'):
                    pass

            if options.user_parameters is not None:
                try:
                    params = json.loads(options.user_parameters)
                except Exception as e:
                    raise ValueError("failed to parse parameters argument as JSON: {}".format(e))
                if not isinstance(params, dict):
                    raise ValueError("failed to parse parameters argument as JSON: not an object")
                self._user_parameters = params
            else:
                self._user_parameters = {}

            if options.user_cmodule is not None:
                self._user_cmodule = os.path.abspath(options.user_cmodule)
                with open(self._user_cmodule, 'rb'):
                    pass
            else:
                self._user_cmodule = None
            self._toolchain = options.toolchain

            self._codeshare_uri = options.codeshare_uri
            self._codeshare_script = None

            self._pending_eval = options.eval_items

            self._quiet = options.quiet
            self._quiet_timeout = float(options.timeout)
            self._on_spawn_complete = options.on_spawn_complete
            self._eternalize = options.eternalize
            self._exit_on_error = options.exit_on_error
            self._autoperform_option = options.autoperform
            self._autoreload = options.autoreload

            if options.logfile is not None:
                self._logfile = codecs.open(options.logfile, 'w', 'utf-8')
            else:
                self._logfile = None

        def _log(self, level, text):
            ConsoleApplication._log(self, level, text)
            if self._logfile is not None:
                self._logfile.write(text + "\n")

        def _usage(self):
            return "%(prog)s [options] target"

        def _needs_target(self):
            return True

        def _start(self):
            self._set_autoperform(self._autoperform_option)
            self._refresh_prompt()

            if self._codeshare_uri is not None:
                self._codeshare_script = self._load_codeshare_script(self._codeshare_uri)
                if self._codeshare_script is None:
                    self._print("Exiting!")
                    self._exit(1)
                    return

            try:
                self._load_script()
            except Exception as e:
                self._update_status("Failed to load script: {error}".format(error=e))
                self._exit(1)
                return

            if self._spawned_argv is not None or self._selected_spawn is not None:
                command = " ".join(self._spawned_argv) if self._spawned_argv is not None else self._selected_spawn.identifier
                if self._on_spawn_complete == 'resume':
                    self._update_status(
                        "Spawned `{command}`. Resuming main thread!".format(command=command))
                    self._do_magic("resume")
                else:
                    self._update_status(
                        "Spawned `{command}`. Use %resume to let the main thread start executing!".format(
                            command=command))
            else:
                self._clear_status()
            self._ready.set()

        def _on_stop(self):
            self._stopping.set()

            if self._cli is not None:
                try:
                    self._cli.app.exit()
                except:
                    pass

        def _stop(self):
            if self._eternalize:
                self._eternalize_script()
            else:
                self._unload_script()

            with frida.Cancellable():
                self._demonitor_all()

            if self._logfile is not None:
                self._logfile.close()

            if not self._quiet:
                self._print("\nThank you for using Frida!")

        def _load_script(self):
            if self._autoreload:
                self._monitor_all()

            if len(self._user_scripts) == 0:
                name = "repl"
            else:
                name = "+".join(map(self._get_script_name, self._user_scripts))

            is_first_load = self._script is None

            script = self._session.create_script(name=name,
                                                 source=self._create_repl_script(),
                                                 runtime=self._runtime)
            script.set_log_handler(self._log)
            self._unload_script()
            self._script = script

            def on_message(message, data):
                self._reactor.schedule(lambda: self._process_message(message, data))

            script.on('message', on_message)
            self._on_script_created(script)
            script.load()

            cmodule_code = self._load_cmodule_code()
            if cmodule_code is not None:
                # TODO: Remove this hack once RPC implementation supports passing binary data in both directions.
                if is_byte_array(cmodule_code):
                    script.post({'type': 'frida:cmodule-payload'}, data=cmodule_code)
                    cmodule_code = None
                script.exports.frida_load_cmodule(cmodule_code, self._toolchain)

            stage = 'early' if self._target[0] == 'file' and is_first_load else 'late'
            try:
                script.exports.init(stage, self._user_parameters)
            except:
                pass

        def _get_script_name(self, path):
            return os.path.splitext(os.path.basename(path))[0]

        def _eternalize_script(self):
            if self._script is None:
                return

            try:
                self._script.eternalize()
            except:
                pass
            self._script = None

        def _unload_script(self):
            if self._script is None:
                return

            try:
                self._script.unload()
            except:
                pass
            self._script = None

        def _monitor_all(self):
            for path in self._user_scripts + [self._user_cmodule]:
                self._monitor(path)

        def _demonitor_all(self):
            for monitor in self._monitored_files.values():
                monitor.disable()
            self._monitored_files = {}

        def _monitor(self, path):
            if path is None or path in self._monitored_files or script_needs_compilation(path):
                return

            monitor = frida.FileMonitor(path)
            monitor.on('change', self._on_change)
            monitor.enable()
            self._monitored_files[path] = monitor

        def _process_input(self, reactor):
            if not self._quiet:
                self._print_startup_message()

            try:
                while self._ready.wait(0.5) != True:
                    if not reactor.is_running():
                        return
            except KeyboardInterrupt:
                self._reactor.cancel_io()
                return

            while True:
                expression = ""
                line = ""
                while len(expression) == 0 or line.endswith("\\"):
                    if not reactor.is_running():
                        return

                    prompt = "[%s]" % self._prompt_string + "-> " if len(expression) == 0 else "... "

                    pending_eval = self._pending_eval
                    if pending_eval is not None:
                        if len(pending_eval) > 0:
                            expression = pending_eval.pop(0)
                            if not self._quiet:
                                self._print(prompt + expression)
                        else:
                            self._pending_eval = None
                    else:
                        if self._quiet:
                            if self._quiet_timeout > 0:
                                if self._quiet_start is None:
                                    self._quiet_start = time.time()
                                passed_time = time.time() - self._quiet_start
                                while self._quiet_timeout > passed_time and reactor.is_running():
                                    sleep_time = min(1, self._quiet_timeout - passed_time)
                                    if self._stopping.wait(sleep_time):
                                        break
                                    if self._dumb_stdin_reader is not None:
                                        with self._dumb_stdin_reader._lock:
                                            if self._dumb_stdin_reader._saw_sigint:
                                                break
                                    passed_time = time.time() - self._quiet_start

                            self._exit_status = 0 if self._errors == 0 else 1
                            return

                        try:
                            if self._cli is not None:
                                line = self._cli.prompt(prompt)
                                if line is None:
                                    return
                            else:
                                line = self._dumb_stdin_reader.read_line(prompt)
                                self._print(line)
                        except EOFError:
                            if not self._have_terminal and os.environ.get("TERM", '') != "dumb":
                                while not self._stopping.wait(1):
                                    pass
                            return
                        except KeyboardInterrupt:
                            line = ""
                            if not self._have_terminal:
                                sys.stdout.write("\n" + prompt)
                            continue
                        if len(line.strip()) > 0:
                            if len(expression) > 0:
                                expression += "\n"
                            expression += line.rstrip("\\")

                if expression.endswith("?"):
                    try:
                        self._print_help(expression)
                    except JavaScriptError as e:
                        error = e.error
                        self._print(Style.BRIGHT + error['name'] + Style.RESET_ALL + ": " + error['message'])
                    except frida.InvalidOperationError:
                        return
                elif expression == "help":
                    self._do_magic("help")
                elif expression in ("exit", "quit", "q"):
                    return
                else:
                    try:
                        if expression.startswith("%"):
                            self._do_magic(expression[1:].rstrip())
                        elif expression.startswith("."):
                            self._do_quick_command(expression[1:].rstrip())
                        else:
                            if self._autoperform:
                                expression = "Java.performNow(() => { return %s\n/**/ });" % expression
                            if not self._exec_and_print(self._evaluate_expression, expression):
                                self._errors += 1
                    except frida.OperationCancelledError:
                        return

        def _get_confirmation(self, question, default_answer = False):
            if default_answer:
                prompt_string = question + " [Y/n] "
            else:
                prompt_string = question + " [y/N] "

            if self._have_terminal and not self._plain_terminal:
                answer = prompt(prompt_string)
            else:
                answer = self._dumb_stdin_reader.read_line(prompt_string)
                self._print(answer)

            if answer.lower() not in ("y", "yes", "n", "no", ""):
                return self._get_confirmation(question, default_answer=default_answer)

            if default_answer:
                return answer.lower() != "n" and answer.lower() != "no"

            return answer.lower() == "y" or answer.lower() == "yes"

        def _exec_and_print(self, exec, arg):
            success = False
            try:
                (t, value) = self._perform_on_reactor_thread(lambda: exec(arg))
                if t in ('function', 'undefined', 'null'):
                    output = t
                elif t == 'binary':
                    output = hexdump(value).rstrip("\n")
                else:
                    output = json.dumps(value, sort_keys=True, indent=4, separators=(",", ": "))
                success = True
            except JavaScriptError as e:
                error = e.error

                output = Fore.RED + Style.BRIGHT + error['name'] + Style.RESET_ALL + ": " + error['message']

                stack = error.get('stack', None)
                if stack is not None:
                    trim_amount = 5 if self._runtime == 'v8' else 6
                    trimmed_stack = stack.split("\n")[1:-trim_amount]
                    if len(trimmed_stack) > 0:
                        output += "\n" + "\n".join(trimmed_stack)
            except frida.InvalidOperationError:
                return success
            if output != "undefined":
                self._print(output)
            return success

        def _print_startup_message(self):
            self._print("""\
     ____
    / _  |   Frida {version} - A world-class dynamic instrumentation toolkit
   | (_| |
    > _  |   Commands:
   /_/ |_|       help      -> Displays the help system
   . . . .       object?   -> Display information about 'object'
   . . . .       exit/quit -> Exit
   . . . .
   . . . .   More info at https://frida.re/docs/home/""".format(version=frida.__version__))

        def _print_help(self, expression):
            # TODO: Figure out docstrings and implement here. This is real jankaty right now.
            help_text = ""
            if expression.endswith(".?"):
                expression = expression[:-2] + "?"

            obj_to_identify = [x for x in expression.split(' ') if x.endswith("?")][0][:-1]
            (obj_type, obj_value) = self._evaluate_expression(obj_to_identify)

            if obj_type == "function":
                signature = self._evaluate_expression("%s.toString()" % obj_to_identify)[1]
                clean_signature = signature.split("{")[0][:-1].split('function ')[-1]

                if "[native code]" in signature:
                    help_text += "Type:      Function (native)\n"
                else:
                    help_text += "Type:      Function\n"

                help_text += "Signature: %s\n" % clean_signature
                help_text += "Docstring: #TODO :)"

            elif obj_type == "object":
                help_text += "Type:      Object\n"
                help_text += "Docstring: #TODO :)"

            elif obj_type == "boolean":
                help_text += "Type:      Boolean\n"
                help_text += "Docstring: #TODO :)"

            elif obj_type == "string":
                help_text += "Type:      Boolean\n"
                help_text += "Text:      %s\n" % self._evaluate_expression("%s.toString()" % obj_to_identify)[1]
                help_text += "Docstring: #TODO :)"

            self._print(help_text)

        # Negative means at least abs(val) - 1
        _magic_command_args = {
            'resume': _repl_magic.Resume(),
            'load': _repl_magic.Load(),
            'reload': _repl_magic.Reload(),
            'unload': _repl_magic.Unload(),
            'autoperform': _repl_magic.Autoperform(),
            'autoreload': _repl_magic.Autoreload(),
            'exec': _repl_magic.Exec(),
            'time': _repl_magic.Time(),
            'help': _repl_magic.Help()
        }

        def _do_magic(self, statement):
            tokens = shlex.split(statement)
            command = tokens[0]
            args = tokens[1:]

            magic_command = self._magic_command_args.get(command)
            if magic_command == None:
                self._print("Unknown command: {}".format(command))
                self._print("Valid commands: {}".format(", ".join(self._magic_command_args.keys())))
                return

            required_args = magic_command.required_args_count
            atleast_args = False
            if required_args < 0:
                atleast_args = True
                required_args = abs(required_args) - 1

            if (not atleast_args and len(args) != required_args) or \
                    (atleast_args and len(args) < required_args):
                self._print("{cmd} command expects {atleast}{n} argument{s}".format(
                    cmd=command, atleast='atleast ' if atleast_args else '', n=required_args,
                    s='' if required_args == 1 else ' '))
                return

            magic_command.execute(self, args)

        def _do_quick_command(self, statement):
            tokens = shlex.split(statement)
            if len(tokens) == 0:
                self._print("Invalid quick command")
                return

            if not self._exec_and_print(self._evaluate_quick_command, tokens):
                self._errors += 1

        def _autoperform_command(self, state_argument):
            if state_argument not in ("on", "off"):
                self._print("autoperform only accepts on and off as parameters")
                return
            self._set_autoperform(state_argument == "on")

        def _set_autoperform(self, state):
            if self._is_java_available():
                self._autoperform = state
                self._refresh_prompt()
            elif state:
                self._print("autoperform is only available in Java processes")

        def _is_java_available(self):
            script = self._session.create_script(name="java_check", source="rpc.exports.javaAvailable = () => Java.available;", runtime=self._runtime)
            script.load()
            try:
                return script.exports.java_available()
            except:
                return False

        def _refresh_prompt(self):
            self._prompt_string = self._create_prompt()

        def _create_prompt(self):
            device_type = self._device.type
            type_name = self._target[0]
            if type_name == 'pid':
                if self._target[1] == 0:
                    target = 'SystemSession'
                else:
                    target = 'PID::%u' % self._target[1]
            elif type_name == 'file':
                target = os.path.basename(self._target[1][0])
            else:
                target = self._target[1]

            suffix = ""
            if self._autoperform:
                suffix = "(ap)"

            if device_type in ('local', 'remote'):
                prompt_string = "%s::%s %s" % (device_type.title(), target, suffix)
            else:
                prompt_string = "%s::%s %s" % (self._device.name, target, suffix)

            return prompt_string

        def _evaluate_expression(self, expression):
            result = self._script.exports.frida_evaluate_expression(expression)
            return self._parse_evaluate_result(result)

        def _evaluate_quick_command(self, tokens):
            result = self._script.exports.frida_evaluate_quick_command(tokens)
            return self._parse_evaluate_result(result)

        def _parse_evaluate_result(self, result):
            if is_byte_array(result):
                return ('binary', result)
            elif isinstance(result, dict):
                return ('binary', bytes())
            elif result[0] == 'error':
                raise JavaScriptError(result[1])
            return result

        def _process_message(self, message, data):
            message_type = message['type']
            if message_type == 'error':
                text = message.get('stack', message['description'])
                self._log('error', text)
                self._errors += 1
                if self._exit_on_error:
                    self._exit(1)
            else:
                self._print("message:", message, "data:", data)

        def _on_change(self, changed_file, other_file, event_type):
            if event_type == 'changes-done-hint':
                return
            self._last_change_id += 1
            change_id = self._last_change_id
            self._reactor.schedule(lambda: self._process_change(change_id), delay=0.05)

        def _process_change(self, change_id):
            if change_id != self._last_change_id:
                return
            self._try_load_script()

        def _try_load_script(self):
            try:
                self._load_script()
            except Exception as e:
                self._print("Failed to load script: {error}".format(error=e))

        def _create_repl_script(self):
            raw_fragments = []

            raw_fragments.append(self._make_repl_runtime())

            if self._codeshare_script is not None:
                raw_fragments.append(self._codeshare_script)

            for user_script in self._user_scripts:
                if script_needs_compilation(user_script):
                    compilation_started = None

                    context = self._compilers.get(user_script, None)
                    if context is None:
                        context = CompilerContext(user_script, self._autoreload, self._on_bundle_updated)
                        context.compiler.on("diagnostics", self._on_compiler_diagnostics)
                        self._compilers[user_script] = context
                        self._update_status(format_compiling(user_script, os.getcwd()))
                        compilation_started = timer()

                    raw_fragments.append(context.get_bundle())

                    if compilation_started is not None:
                        compilation_finished = timer()
                        self._update_status(format_compiled(user_script, os.getcwd(), compilation_started, compilation_finished))
                else:
                    with codecs.open(user_script, 'rb', 'utf-8') as f:
                        raw_fragments.append(f.read())

            fragments = []
            next_script_id = 1
            for raw_fragment in raw_fragments:
                if raw_fragment.startswith("📦\n"):
                    fragments.append(raw_fragment[2:])
                else:
                    script_id = next_script_id
                    next_script_id += 1
                    size = len(raw_fragment.encode("utf-8"))
                    fragments.append("{} /frida/repl-{}.js\n✄\n{}".format(size, script_id, raw_fragment))

            return "📦\n" + "\n✄\n".join(fragments)

        def _on_bundle_updated(self):
            self._reactor.schedule(lambda: self._try_load_script())

        def _on_compiler_diagnostics(self, diagnostics):
            self._reactor.schedule(lambda: self._print_compiler_diagnostics(diagnostics))

        def _print_compiler_diagnostics(self, diagnostics):
            cwd = os.getcwd()
            for diag in diagnostics:
                self._print(format_diagnostic(diag, cwd))

        def _make_repl_runtime(self):
            return """\
global.cm = null;
global.cs = {};

class REPL {
    #quickCommands;
    constructor() {
        this.#quickCommands = new Map();
    }
    registerQuickCommand(name, handler) {
        this.#quickCommands.set(name, handler);
    }
    unregisterQuickCommand(name) {
        this.#quickCommands.delete(name);
    }
    _invokeQuickCommand(tokens) {
        const name = tokens[0];
        const handler = this.#quickCommands.get(name);
        if (handler !== undefined) {
            const { minArity, onInvoke } = handler;
            if (tokens.length - 1 < minArity) {
                throw Error(`${name} needs at least ${minArity} arg${(minArity === 1) ? '' : 's'}`);
            }
            return onInvoke(...tokens.slice(1));
        } else {
            throw Error(`Unknown command ${name}`);
        }
    }
}
const repl = new REPL();
global.REPL = repl;

const rpcExports = {
    fridaEvaluateExpression(expression) {
        return evaluate(() => (1, eval)(expression));
    },
    fridaEvaluateQuickCommand(tokens) {
        return evaluate(() => repl._invokeQuickCommand(tokens));
    },
    fridaLoadCmodule(code, toolchain) {
        const cs = global.cs;

        if (cs._frida_log === undefined)
            cs._frida_log = new NativeCallback(onLog, 'void', ['pointer']);

        if (code === null) {
            recv('frida:cmodule-payload', (message, data) => {
                code = data;
            });
        }

        global.cm = new CModule(code, cs, { toolchain });
    },
};

function evaluate(func) {
    try {
        const result = func();
        if (result instanceof ArrayBuffer) {
            return result;
        } else {
            const type = (result === null) ? 'null' : typeof result;
            return [type, result];
        }
    } catch (e) {
        return ['error', {
            name: e.name,
            message: e.message,
            stack: e.stack
        }];
    }
}

Object.defineProperty(rpc, 'exports', {
    get() {
        return rpcExports;
    },
    set(value) {
        for (const [k, v] of Object.entries(value)) {
            rpcExports[k] = v;
        }
    }
});

function onLog(messagePtr) {
    const message = messagePtr.readUtf8String();
    console.log(message);
}
"""

        def _load_cmodule_code(self):
            if self._user_cmodule is None:
                return None

            with open(self._user_cmodule, 'rb') as f:
                code = f.read()
            if code_is_native(code):
                return code
            source = code.decode('utf-8')

            name = os.path.basename(self._user_cmodule)

            return """static void frida_log (const char * format, ...);\n#line 1 "{name}"\n""".format(name=name) + source + """\
#line 1 "frida-repl-builtins.c"
#include <glib.h>

extern void _frida_log (const gchar * message);

static void
frida_log (const char * format,
           ...)
{
  gchar * message;
  va_list args;

  va_start (args, format);
  message = g_strdup_vprintf (format, args);
  va_end (args);

  _frida_log (message);

  g_free (message);
}
"""

        def _load_codeshare_script(self, uri):
            trust_store = self._get_or_create_truststore()

            project_url = "https://codeshare.frida.re/api/project/{}/".format(uri)
            response_json = None
            try:
                request = build_opener()
                request.addheaders = [('User-Agent', 'Frida v{} | {}'.format(frida.__version__, platform.platform()))]
                response = request.open(project_url)
                response_content = response.read().decode('utf-8')
                response_json = json.loads(response_content)
            except Exception as e:
                self._print("Got an unhandled exception while trying to retrieve {} - {}".format(uri, e))
                return None

            trusted_signature = trust_store.get(uri, "")
            fingerprint = hashlib.sha256(response_json['source'].encode('utf-8')).hexdigest()
            if fingerprint == trusted_signature:
                return response_json['source']

            self._print("""Hello! This is the first time you're running this particular snippet, or the snippet's source code has changed.

Project Name: {project_name}
Author: {author}
Slug: {slug}
Fingerprint: {fingerprint}
URL: {url}
            """.format(
                project_name=response_json['project_name'],
                author="@" + uri.split('/')[0],
                slug=uri,
                fingerprint=fingerprint,
                url="https://codeshare.frida.re/@{}".format(uri)
            ))

            answer = self._get_confirmation("Are you sure you'd like to trust this project?")
            if answer:
                self._print(
                    "Adding fingerprint {} to the trust store! You won't be prompted again unless the code changes.".format(
                        fingerprint))
                script = response_json['source']
                self._update_truststore({
                    uri: fingerprint
                })
                return script

        def _update_truststore(self, record):
            trust_store = self._get_or_create_truststore()
            trust_store.update(record)

            codeshare_trust_store = self._get_or_create_truststore_file()

            with open(codeshare_trust_store, 'w') as f:
                f.write(json.dumps(trust_store))

        def _get_or_create_truststore(self):
            codeshare_trust_store = self._get_or_create_truststore_file()

            if os.path.exists(codeshare_trust_store):
                try:
                    with open(codeshare_trust_store) as f:
                        trust_store = json.load(f)
                except Exception as e:
                    self._print(
                        "Unable to load the codeshare truststore ({}), defaulting to an empty truststore. You will be prompted every time you want to run a script!".format(
                            e))
                    trust_store = {}
            else:
                with open(codeshare_trust_store, 'w') as f:
                    f.write(json.dumps({}))
                trust_store = {}

            return trust_store

        def _get_or_create_truststore_file(self):
            truststore_file = os.path.join(self._get_or_create_data_dir(), 'codeshare-truststore.json')
            if not os.path.isfile(truststore_file):
                self._migrate_old_config_file('codeshare-truststore.json', truststore_file)
            return truststore_file

        def _get_or_create_history_file(self):
            history_file = os.path.join(self._get_or_create_state_dir(), 'history')
            if os.path.isfile(history_file):
                return history_file

            found_old = self._migrate_old_config_file('history', history_file)
            if not found_old:
                open(history_file, 'a').close()

            return history_file

        def _migrate_old_config_file(self, name, new_path):
            xdg_config_home = os.getenv("XDG_CONFIG_HOME")
            if xdg_config_home is not None:
                old_file = os.path.exists(os.path.join(xdg_config_home, 'frida', name))
                if os.path.isfile(old_file):
                    os.rename(old_file, new_path)
                    return True

            old_file = os.path.join(os.path.expanduser('~'), '.frida', name)
            if os.path.isfile(old_file):
                os.rename(old_file, new_path)
                return True

            return False

        def _on_device_found(self):
            if not self._quiet:
                self._print("""\
   . . . .
   . . . .   Connected to {device_name} (id={device_id})""".format(device_id=self._device.id, device_name=self._device.name))

    class CompilerContext:
        def __init__(self, user_script, autoreload, on_bundle_updated):
            self._user_script = user_script
            self._autoreload = autoreload
            self._on_bundle_updated = on_bundle_updated

            self.compiler = frida.Compiler()
            self._bundle = None

        def get_bundle(self):
            compiler = self.compiler

            if not self._autoreload:
                return compiler.build(self._user_script)

            if self._bundle is None:
                ready = threading.Event()

                def on_compiler_output(bundle):
                    self._bundle = bundle
                    ready.set()
                    self._on_bundle_updated()

                compiler.on("output", on_compiler_output)
                compiler.watch(self._user_script)
                ready.wait()

            return self._bundle

    class FridaCompleter(Completer):
        def __init__(self, repl):
            self._repl = repl
            self._lexer = JavascriptLexer()

        def get_completions(self, document, complete_event):
            prefix = document.text_before_cursor

            magic = len(prefix) > 0 and prefix[0] == '%' and not any(map(lambda c: c.isspace(), prefix))

            tokens = list(self._lexer.get_tokens(prefix))[:-1]

            # 0.toString() is invalid syntax,
            # but pygments doesn't seem to know that
            for i in range(len(tokens) - 1):
                if tokens[i][0] == Token.Literal.Number.Integer \
                        and tokens[i + 1][0] == Token.Punctuation and tokens[i + 1][1] == '.':
                    tokens[i] = (Token.Literal.Number.Float, tokens[i][1] + tokens[i + 1][1])
                    del tokens[i + 1]

            before_dot = ''
            after_dot = ''
            encountered_dot = False
            for t in tokens[::-1]:
                if t[0] in Token.Name.subtypes:
                    before_dot = t[1] + before_dot
                elif t[0] == Token.Punctuation and t[1] == '.':
                    before_dot = '.' + before_dot
                    if not encountered_dot:
                        encountered_dot = True
                        after_dot = before_dot[1:]
                        before_dot = ''
                else:
                    if encountered_dot:
                        # The value/contents of the string, number or array doesn't matter,
                        # so we just use the simplest value with that type
                        if t[0] in Token.Literal.String.subtypes:
                            before_dot = '""' + before_dot
                        elif t[0] in Token.Literal.Number.subtypes:
                            before_dot = '0.0' + before_dot
                        elif t[0] == Token.Punctuation and t[1] == ']':
                            before_dot = '[]' + before_dot
                        elif t[0] == Token.Punctuation and t[1] == ')':
                            # we don't know the returned value of the function call so we abort the completion
                            return

                    break

            try:
                if encountered_dot:
                    if before_dot == "" or before_dot.endswith("."):
                        return
                    for key in self._get_keys("""\
                            (() => {
                                let o;
                                try {
                                    o = """ + before_dot + """;
                                } catch (e) {
                                    return [];
                                }

                                if (o === undefined || o === null)
                                    return [];

                                let k = Object.getOwnPropertyNames(o);

                                let p;
                                if (typeof o !== 'object')
                                    p = o.__proto__;
                                else
                                    p = Object.getPrototypeOf(o);
                                if (p !== null && p !== undefined)
                                    k = k.concat(Object.getOwnPropertyNames(p));

                                return k;
                            })();"""):
                        if self._pattern_matches(after_dot, key):
                            yield Completion(key, -len(after_dot))
                else:
                    if magic:
                        keys = self._repl._magic_command_args.keys()
                    else:
                        keys = self._get_keys("Object.getOwnPropertyNames(this)")
                    for key in keys:
                        if not self._pattern_matches(before_dot, key) or (key.startswith('_') and before_dot == ''):
                            continue
                        yield Completion(key, -len(before_dot))
            except frida.InvalidOperationError:
                pass
            except frida.OperationCancelledError:
                pass
            except Exception as e:
                self._repl._print(e)

        def _get_keys(self, code):
            repl = self._repl
            with repl._reactor.io_cancellable:
                (t, value) = repl._evaluate_expression(code)

            if t == 'error':
                return []

            return sorted(filter(self._is_valid_name, set(value)))

        def _is_valid_name(self, name):
            tokens = list(self._lexer.get_tokens(name))
            return len(tokens) == 2 and tokens[0][0] in Token.Name.subtypes

        def _pattern_matches(self, pattern, text):
            return re.search(re.escape(pattern), text, re.IGNORECASE) != None

    def script_needs_compilation(path):
        return path.endswith(".ts")

    def hexdump(src, length=16):
        try:
            xrange
        except NameError:
            xrange = range
        FILTER = "".join([(len(repr(chr(x))) == 3) and chr(x) or "." for x in range(256)])
        lines = []
        for c in xrange(0, len(src), length):
            chars = src[c:c + length]
            hex = " ".join(["%02x" % x for x in iterbytes(chars)])
            printable = ''.join(["%s" % ((x <= 127 and FILTER[x]) or ".") for x in iterbytes(chars)])
            lines.append("%04x  %-*s  %s\n" % (c, length * 3, hex, printable))
        return "".join(lines)

    def is_byte_array(value):
        if sys.version_info[0] >= 3:
            return isinstance(value, bytes)
        else:
            return isinstance(value, str)

    if sys.version_info[0] >= 3:
        iterbytes = lambda x: iter(x)
    else:
        def iterbytes(data):
            return (ord(char) for char in data)

    OS_BINARY_SIGNATURES = set([
        b"\x4d\x5a",         # PE
        b"\xca\xfe\xba\xbe", # Fat Mach-O
        b"\xcf\xfa\xed\xfe", # Mach-O
        b"\x7fELF",          # ELF
    ])

    def code_is_native(code):
        return (code[:4] in OS_BINARY_SIGNATURES) or (code[:2] in OS_BINARY_SIGNATURES)

    app = REPLApplication()
    app.run()


class JavaScriptError(Exception):
    def __init__(self, error):
        super(JavaScriptError, self).__init__(error['message'])

        self.error = error


class DumbStdinReader(object):
    def __init__(self, valid_until):
        self._valid_until = valid_until

        self._saw_sigint = False
        self._prompt = None
        self._result = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

        try:
            self._get_input = raw_input
        except NameError:
            self._get_input = input

        worker = threading.Thread(target=self._process_requests, name="stdin-reader")
        worker.daemon = True
        worker.start()

        signal.signal(signal.SIGINT, lambda n, f: self._cancel_line())

    def read_line(self, prompt_string):
        with self._lock:
            self._prompt = prompt_string
            self._cond.notify()

        with self._lock:
            while self._result is None:
                if self._valid_until():
                    raise EOFError()
                self._cond.wait(1)
            line, error = self._result
            self._result = None

        if error is not None:
            raise error

        return line

    def _process_requests(self):
        error = None
        while error is None:
            with self._lock:
                while self._prompt is None:
                    self._cond.wait()
                prompt = self._prompt

            try:
                line = self._get_input(prompt)
            except Exception as e:
                line = None
                error = e

            with self._lock:
                self._prompt = None
                self._result = (line, error)
                self._cond.notify()

    def _cancel_line(self):
        with self._lock:
            self._saw_sigint = True
            self._prompt = None
            self._result = (None, KeyboardInterrupt())
            self._cond.notify()


if os.environ.get("TERM", "") == 'dumb':
    try:
        from collections import namedtuple
        from epc.client import EPCClient
    except ImportError:
        def start_completion_thread(repl, epc_port=None):
            # Do nothing when we cannot import the EPC module.
            _, _ = repl, epc_port
    else:
        class EPCCompletionClient(EPCClient):
            def __init__(self, address="localhost", port=None, *args, **kargs):
                if port is not None:
                    args = ((address, port),) + args
                EPCClient.__init__(self, *args, **kargs)

                def complete(*cargs, **ckargs):
                    return self.complete(*cargs, **ckargs)
                self.register_function(complete)

        EpcDocument = namedtuple('Document', ['text_before_cursor',])

        SYMBOL_CHARS = "._" + string.ascii_letters + string.digits
        FIRST_SYMBOL_CHARS = "_" + string.ascii_letters
        class ReplEPCCompletion(object):
            def __init__(self, repl, *args, **kargs):
                _, _ = args, kargs
                self._repl = repl

            def complete(self, *to_complete):
                to_complete = "".join(to_complete)
                prefix = ''
                if len(to_complete) != 0:
                    for i, x in enumerate(to_complete[::-1]):
                        if x not in SYMBOL_CHARS:
                            while i >= 0 and to_complete[-i] not in FIRST_SYMBOL_CHARS:
                                i -= 1
                            prefix, to_complete = to_complete[:-i], to_complete[-i:]
                            break
                pos = len(prefix)
                if "." in to_complete:
                    prefix += to_complete.rsplit(".", 1)[0] + "."
                try:
                    completions = self._repl._completer.get_completions(
                        EpcDocument(text_before_cursor=to_complete), None)
                except Exception as ex:
                    _ = ex
                    return tuple()
                completions = [
                    {
                        "word": prefix + c.text,
                        "pos": pos,
                    }
                    for c in completions
                ]
                return tuple(completions)

        class ReplEPCCompletionClient(EPCCompletionClient, ReplEPCCompletion):
            def __init__(self, repl, *args, **kargs):
                EPCCompletionClient.__init__(self, *args, **kargs)
                ReplEPCCompletion.__init__(self, repl)

        def start_completion_thread(repl, epc_port=None):
            if epc_port is None:
                epc_port = os.environ.get("EPC_COMPLETION_SERVER_PORT", None)
            rpc_complete_thread = None
            if epc_port is not None:
                epc_port = int(epc_port)
                rpc_complete = ReplEPCCompletionClient(repl, port=epc_port)
                rpc_complete_thread = threading.Thread(
                    target=rpc_complete.connect,
                    name="PythonModeEPCCompletion",
                    kwargs={'socket_or_address': ("localhost", epc_port)})
            if rpc_complete_thread is not None:
                rpc_complete_thread.daemon = True
                rpc_complete_thread.start()
                return rpc_complete_thread
else:
    def start_completion_thread(repl, epc_port=None):
        # Do nothing as completion-epc is not needed when not running in Emacs.
        _, _ = repl, epc_port


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
