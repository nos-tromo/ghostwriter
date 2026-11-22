export const en = {
  // the page
  'app.title': 'Ghostwriter',

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
  'chat.delete_files': "The chat's files could not be deleted; try again.",

  // conversation
  'chat.empty': 'Ask anything, or attach a document.',
  'chat.empty_text_only': 'Ask anything.',
  'chat.loading_messages': 'Loading the chat…',
  'chat.answer_pending': 'Working on an answer…',
  'chat.thinking': 'thinking…',
  'chat.regenerate': 'Regenerate',
  'chat.copy_answer': 'Copy answer',
  'chat.copy_code': 'Copy code',
  'chat.answer_busy': 'An answer is still being written in this chat.',
  'chat.no_answer': 'There is no answer to regenerate.',
  'chat.images_dropped': 'The model did not take the pictures; it answered without them.',
  'chat.attachment_processing': 'Wait until the attached files are ready.',

  // the reasoning switch
  'chat.reasoning_state': 'Reasoning: {mode}',
  'chat.reasoning_on': 'On — the model thinks before answering',
  'chat.reasoning_off': 'Off — faster, plain answer',

  // the composer
  'composer.placeholder': 'Write a message',
  'composer.attach': 'Attach files',
  'composer.drop': 'Drop files to attach them',
  'composer.files_stay': 'Attached files stay in this chat until it is deleted, for {days} days at most.',
  'composer.uploading': 'Attaching {name}…',
  'composer.remove': 'Remove {name}',
  'composer.error_type': '{name}: this file type is not supported.',
  'composer.error_size': '{name} is larger than 50 MB.',
  'composer.error_upload': '{name} could not be attached.',
  'composer.error_storage_full':
    'Your files fill the space workbench allows; delete a chat or wait for files to expire.',
  'composer.error_purpose': '{name} was not attached: attaching files needs a stated purpose.',
  'composer.error_too_many_pending': 'Send the attached files before adding more.',
  'composer.processing': 'Preparing {name}…',
  'composer.removing': 'Removing {name}…',
  'composer.error_remove': '{name} could not be removed.',
  'composer.processing_hint': 'Sending waits until the attached files are ready.',
  'composer.unreadable': '{name} could not be read; the model will be told.',
  'composer.images_cap':
    'At most {max} images go to the model with one message; the others are only named.',
  'composer.send': 'Send',
  'composer.stop': 'Stop',

  // a picture at full size
  'lightbox.close': 'Close',

  // dictation
  'mic.start': 'Dictate',
  'mic.stop': 'Stop dictating',
  'mic.denied': 'Microphone access was denied.',
  'mic.unsupported': 'This browser cannot record audio.',
  'mic.unavailable': 'No microphone could be used.',
  'mic.limit': '{s} s left',

  // the trail of tool steps
  'trail.steps': '{n} steps',
  'trail.steps_one': '1 step',
  'trail.list_files': 'Listed the files',
  'trail.read_document': 'Read {detail}',
  'trail.read_document_unnamed': 'Read a document',
  'trail.view_file': 'Looked at {detail}',
  'trail.view_file_unnamed': 'Looked at a file',
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
  'trail.write_presentation': 'Built a presentation from {detail}',
  'trail.write_presentation_unnamed': 'Built a presentation',
  'trail.other': 'Used {tool}',
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
  'result.download_named': 'Download {name}',
  'result.loading': 'Loading {name}…',

  // why an answer ended early
  'error.model': 'The model did not answer. Try again.',
  'error.audit': 'The answer stopped: it could not be recorded.',
  'error.internal': 'Something went wrong.',

  // retention note
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
