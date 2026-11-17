export const en = {
  // the page
  'app.title': 'Ghostwriter · Chat assistant',

  // appheader
  'appheader.home': 'Apps',
  'appheader.theme_system': 'system',
  'appheader.theme_light': 'light',
  'appheader.theme_dark': 'dark',
  'appheader.sign_out': 'Sign out',
  'appheader.sidebar_toggle': 'Toggle sidebar',

  // chat list
  'chat.list': 'Chats',
  'chat.list_empty': 'No chats yet.',
  'chat.loading': 'Loading chats…',
  'chat.new': 'New chat',
  'chat.untitled': 'New chat',
  'chat.options': 'Options for {title}',
  'chat.rename': 'Rename',
  'chat.title': 'Chat title',
  'chat.delete': 'Delete',
  'chat.delete_confirm': 'Delete this chat and its files?',
  'chat.delete_busy': 'Stop the answer before deleting the chat.',

  // conversation
  'chat.empty': 'Ask anything, or attach a document.',
  'chat.empty_text_only': 'Ask anything.',
  'chat.loading_messages': 'Loading the chat…',
  'chat.answer_pending': 'Working on an answer…',
  'chat.thinking': 'thinking…',
  'chat.regenerate': 'Regenerate',
  'chat.answer_busy': 'An answer is still being written in this chat.',

  // the trail of tool steps
  'trail.steps': '{n} steps',
  'trail.steps_one': '1 step',
  'trail.list_files': 'Listed the files',
  'trail.read_document': 'Read {detail}',
  'trail.read_document_unnamed': 'Read a document',
  'trail.search_files': 'Searched for {detail}',
  'trail.search_files_unnamed': 'Searched the files',
  'trail.run_python': 'Ran Python',
  'trail.inspect_template': 'Inspected the template {detail}',
  'trail.inspect_template_unnamed': 'Inspected a template',
  'trail.fill_template': 'Filled the template {detail}',
  'trail.fill_template_unnamed': 'Filled a template',
  'trail.write_table': 'Wrote the table {detail}',
  'trail.write_table_unnamed': 'Wrote a table',
  'trail.write_document': 'Wrote the document {detail}',
  'trail.write_document_unnamed': 'Wrote a document',
  'trail.other': 'Used {tool}',
  'trail.details': 'Details',
  'trail.expired': 'Details deleted after {days} days',
  'trail.status_running': 'Running',
  'trail.status_ok': 'Done',
  'trail.status_error': 'Failed',

  // result cards
  'result.more_rows': '{count} more rows',
  'result.more_rows_one': '1 more row',
  'result.more_rows_unknown': 'More rows in the file',
  'result.expired': 'Expired',
  'result.download': 'Download',
  'result.loading': 'Loading {name}…',

  // why an answer ended early
  'error.model': 'The model did not answer. Try again.',
  'error.audit': 'The answer stopped: it could not be recorded.',
  'error.internal': 'Something went wrong.',

  // retention banner
  'retention.chats': 'Chats are deleted {days} days after they were started.',
  'retention.files': 'Files and tool results are deleted after {days} days.',

  // purpose (PurposeBar; {max} is filled in by the bar)
  'purpose.label': 'Purpose',
  'purpose.empty': 'No purpose stated',
  'purpose.required': 'Purpose required',
  'purpose.title': 'Purpose',
  'purpose.description':
    'Sent with each request you make in this app until you change or clear it, or the session ends.',
  'purpose.requested': 'This action needs a stated purpose.',
  'purpose.case_ref': 'Case reference',
  'purpose.reason': 'Reason',
  'purpose.save': 'Save',
  'purpose.cancel': 'Cancel',
  'purpose.clear': 'Clear',
  'purpose.error_length': 'At most {max} characters.',
  'purpose.error_control': 'Remove line breaks, tabs and other control characters.',
  'purpose.error_header': 'Too long to send together. Shorten one of the fields.',
  'purpose.error_empty': 'Enter a case reference or a reason.',

  // common
  'common.cancel': 'Cancel',
  'common.copied': 'Copied',
  'common.error_request': 'Something went wrong ({status}). Please try again or contact support.',
  'common.error_unknown': 'Something went wrong. Please try again or contact support.',
  'common.error_network': 'Service unreachable. Please check your connection or try again later.',
  'common.error_purpose_required':
    'This action needs a stated purpose. Please state one in the header and try again.',
}
