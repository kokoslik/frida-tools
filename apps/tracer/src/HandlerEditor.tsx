import { HandlerId } from "./model.js";
import Editor from "@monaco-editor/react";
import type monaco from "monaco-editor";
import { useEffect, useState } from "react";

export interface HandlerEditorProps {
    handlerId: HandlerId;
    handlerCode: string;
    onChange: CodeEventHandler;
    onSave: CodeEventHandler;
}

export type CodeEventHandler = (code: string) => void;

const USE_META_KEY = navigator.platform.indexOf("Mac") === 0 || navigator.platform === "iPhone";

export default function HandlerEditor({ handlerId, handlerCode, onChange, onSave }: HandlerEditorProps) {
    const [editor, setEditor] = useState<monaco.editor.IStandaloneCodeEditor | null>(null);
    const [monaco, setMonaco] = useState<any>(null);

    const editorOptions: monaco.editor.IStandaloneEditorConstructionOptions = {
        automaticLayout: true,
        readOnly: handlerId === -1,
        readOnlyMessage: { value: "Cannot edit without a handler selected" },
    };

    function handleEditorDidMount(editor: monaco.editor.IStandaloneCodeEditor, monaco: any) {
        setEditor(editor);
        setMonaco(monaco);
    }

    useEffect(() => {
        if (monaco === null) {
            return;
        }

        const callback = editor!.onKeyDown(e => {
            if ((USE_META_KEY ? e.metaKey : e.ctrlKey) && e.keyCode === monaco.KeyCode.KeyS) {
                onSave(editor!.getValue());
                e.preventDefault();
            }
        });

        return () => {
            callback.dispose();
        };
    }, [onSave, editor, monaco]);

    return (
        <Editor
            className="editor"
            value={handlerCode}
            width=""
            height=""
            language="typescript"
            theme="vs-dark"
            options={editorOptions}
            beforeMount={handleEditorWillMount}
            onMount={handleEditorDidMount}
            onValidate={() => onChange(editor!.getValue())}
        />
    );
}

async function handleEditorWillMount(monaco: any) {
    const typingsResponse = await fetch("https://raw.githubusercontent.com/DefinitelyTyped/DefinitelyTyped/master/types/frida-gum/index.d.ts");
    const typingsContent = await typingsResponse.text();
    monaco.languages.typescript.typescriptDefaults.addExtraLib(typingsContent + `
      declare function defineHandler(handler: TraceScriptHandler): TraceScriptHandler;
  
      interface TraceScriptHandler {
        /**
         * Called synchronously when about to call the traced function.
         *
         * @this {object} - Object allowing you to store state for use in onLeave.
         * @param {function} log - Call this function with a string to be presented to the user.
         * @param {array} args - Function arguments represented as an array of NativePointer objects.
         * For example use args[0].readUtf8String() if the first argument is a pointer to a C string encoded as UTF-8.
         * It is also possible to modify arguments by assigning a NativePointer object to an element of this array.
         * @param {object} state - Object allowing you to keep state across function calls.
         * Only one JavaScript function will execute at a time, so do not worry about race-conditions.
         * However, do not use this to store function arguments across onEnter/onLeave, but instead
         * use "this" which is an object for keeping state local to an invocation.
         */
        onEnter?(this: InvocationContext, log: TraceLogFunction, args: InvocationArguments, state: TraceScriptState): void;
  
        /**
         * Called synchronously when about to return from the traced function.
         *
         * See onEnter for details.
         *
         * @this {object} - Object allowing you to access state stored in onEnter.
         * @param {function} log - Call this function with a string to be presented to the user.
         * @param {NativePointer} retval - Return value represented as a NativePointer object.
         * @param {object} state - Object allowing you to keep state across function calls.
         */
        onLeave?(this: InvocationContext, log: TraceLogFunction, retval: InvocationReturnValue, state: TraceScriptState): void;
      }
  
      type TraceLogFunction = (...args: any[]) => void;
  
      interface TraceScriptState {
        [x: string]: any;
      }
    `, "");
}
