/**
 * Sequence-step body editor: bold / italic / underline / link on a small
 * allowlist of HTML. Variable chips call insert() so {{placeholders}} stay
 * literal text the backend renderer already understands.
 */

import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react';
import { EditorContent, useEditor } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import Underline from '@tiptap/extension-underline';
import Link from '@tiptap/extension-link';

const PLACEHOLDER = 'Body — use variables for anything candidate-specific';

const HTML_HINT = /<\/?(?:p|br|strong|b|em|i|u|a)(?:\s|>|\/)/i;

export function looksLikeHtml(value) {
  return HTML_HINT.test(value || '');
}

export function toEditorHtml(value) {
  if (!value) return '';
  if (looksLikeHtml(value)) return value;
  const escaped = value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  return escaped
    .split(/\n\n+/)
    .filter((part) => part.length)
    .map((part) => `<p>${part.replace(/\n/g, '<br>')}</p>`)
    .join('');
}

function ToolbarButton({ editor, action, label, active, onClick }) {
  return (
    <button
      type="button"
      className={active ? 'is-active' : ''}
      aria-pressed={active}
      aria-label={label}
      title={label}
      onMouseDown={(event) => event.preventDefault()}
      onClick={onClick}
      disabled={!editor}
    >
      {action}
    </button>
  );
}

export const EmailBodyEditor = forwardRef(function EmailBodyEditor(
  { value, onChange, onFocus },
  ref,
) {
  const lastEmitted = useRef(null);
  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: false,
        bulletList: false,
        orderedList: false,
        listItem: false,
        blockquote: false,
        codeBlock: false,
        code: false,
        horizontalRule: false,
        strike: false,
      }),
      Underline,
      Link.configure({
        openOnClick: false,
        autolink: true,
        defaultProtocol: 'https',
        validate: (href) => /^https?:\/\//i.test(href),
        HTMLAttributes: { rel: 'noopener noreferrer', target: '_blank' },
      }),
    ],
    content: toEditorHtml(value),
    editorProps: {
      attributes: {
        class: 'tiptap',
        'data-testid': 'email-body-editor',
        'aria-label': PLACEHOLDER,
      },
    },
    onUpdate: ({ editor: instance }) => {
      const html = instance.getHTML();
      lastEmitted.current = html;
      onChange(html);
    },
    onFocus: () => onFocus?.(),
  });

  useImperativeHandle(
    ref,
    () => ({
      insert(snippet) {
        editor?.chain().focus().insertContent(snippet).run();
      },
    }),
    [editor],
  );

  useEffect(() => {
    if (!editor) return;
    if (value === lastEmitted.current) return;
    const next = toEditorHtml(value);
    if (next === editor.getHTML()) return;
    editor.commands.setContent(next, false);
  }, [editor, value]);

  function toggleLink() {
    if (!editor) return;
    if (editor.isActive('link')) {
      editor.chain().focus().unsetLink().run();
      return;
    }
    const previous = editor.getAttributes('link').href || 'https://';
    const url = window.prompt('Link URL', previous);
    if (url === null) return;
    const trimmed = url.trim();
    if (!trimmed) {
      editor.chain().focus().unsetLink().run();
      return;
    }
    editor.chain().focus().extendMarkRange('link').setLink({ href: trimmed }).run();
  }

  return (
    <div className="email-body-editor">
      <div className="email-body-toolbar" role="toolbar" aria-label="Text formatting">
        <ToolbarButton
          editor={editor}
          action="B"
          label="Bold"
          active={editor?.isActive('bold')}
          onClick={() => editor.chain().focus().toggleBold().run()}
        />
        <ToolbarButton
          editor={editor}
          action="I"
          label="Italic"
          active={editor?.isActive('italic')}
          onClick={() => editor.chain().focus().toggleItalic().run()}
        />
        <ToolbarButton
          editor={editor}
          action="U"
          label="Underline"
          active={editor?.isActive('underline')}
          onClick={() => editor.chain().focus().toggleUnderline().run()}
        />
        <ToolbarButton
          editor={editor}
          action="Link"
          label="Link"
          active={editor?.isActive('link')}
          onClick={toggleLink}
        />
      </div>
      <div className="email-body-surface">
        <EditorContent editor={editor} />
        {editor?.isEmpty ? <div className="email-body-placeholder">{PLACEHOLDER}</div> : null}
      </div>
    </div>
  );
});
