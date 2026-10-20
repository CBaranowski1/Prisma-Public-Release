import { basicSetup } from 'codemirror';
import { Compartment, EditorState } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';
import { indentWithTab } from '@codemirror/commands';
import { javascript } from '@codemirror/lang-javascript';
import { python } from '@codemirror/lang-python';
import { rust } from '@codemirror/lang-rust';
import { html } from '@codemirror/lang-html';
import { css } from '@codemirror/lang-css';
import { sql } from '@codemirror/lang-sql';
import { json } from '@codemirror/lang-json';
import { markdown } from '@codemirror/lang-markdown';
import { yaml } from '@codemirror/lang-yaml';
import { diffLines } from 'diff';

const languageExtension = (language) => {
  switch (language) {
    case 'javascript':
    case 'jsx':
      return javascript({ jsx: true });
    case 'typescript':
      return javascript({ typescript: true, jsx: true });
    case 'python':
      return python();
    case 'rust':
      return rust();
    case 'html':
      return html();
    case 'css':
      return css();
    case 'sql':
      return sql();
    case 'json':
      return json();
    case 'markdown':
      return markdown();
    case 'yaml':
      return yaml();
    default:
      return [];
  }
};

const prismaTheme = EditorView.theme(
  {
    '&': {
      height: '100%',
      color: '#d8dee9',
      backgroundColor: '#11151c',
      fontSize: '13px',
    },
    '.cm-content': {
      caretColor: '#7dd3fc',
      fontFamily:
        "'Cascadia Code', 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace",
      padding: '10px 0 40px',
    },
    '.cm-cursor, .cm-dropCursor': { borderLeftColor: '#7dd3fc' },
    '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection':
      { backgroundColor: '#23405b' },
    '.cm-gutters': {
      backgroundColor: '#0d1117',
      color: '#52606f',
      border: 'none',
      borderRight: '1px solid #202733',
    },
    '.cm-activeLine': { backgroundColor: '#171d26' },
    '.cm-activeLineGutter': { backgroundColor: '#171d26', color: '#94a3b8' },
    '.cm-scroller': { overflow: 'auto' },
    '.cm-tooltip': {
      backgroundColor: '#161d27',
      border: '1px solid #303b4b',
    },
  },
  { dark: true },
);

export function createModelIdeEditor(parent, options = {}) {
  const language = new Compartment();
  let suppressChanges = false;
  const state = EditorState.create({
    doc: options.content || '',
    extensions: [
      basicSetup,
      keymap.of([indentWithTab]),
      prismaTheme,
      language.of(languageExtension(options.language)),
      EditorView.updateListener.of((update) => {
        if (update.docChanged && !suppressChanges && options.onChange) {
          options.onChange(update.state.doc.toString());
        }
      }),
    ],
  });
  const view = new EditorView({ state, parent });

  return {
    getValue() {
      return view.state.doc.toString();
    },
    setValue(value) {
      suppressChanges = true;
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: value || '' },
      });
      suppressChanges = false;
    },
    setLanguage(value) {
      view.dispatch({ effects: language.reconfigure(languageExtension(value)) });
    },
    focus() {
      view.focus();
    },
    goToLine(value) {
      const requested = Number.parseInt(value, 10);
      if (!Number.isFinite(requested)) return;
      const lineNumber = Math.max(1, Math.min(requested, view.state.doc.lines));
      const position = view.state.doc.line(lineNumber).from;
      view.dispatch({
        selection: { anchor: position },
        effects: EditorView.scrollIntoView(position, { y: 'center' }),
      });
      view.focus();
    },
    destroy() {
      view.destroy();
    },
  };
}

export function buildLineDiff(before, after) {
  return diffLines(before || '', after || '').map((part) => ({
    type: part.added ? 'added' : part.removed ? 'removed' : 'context',
    count: part.count || 0,
    value: part.value,
  }));
}
