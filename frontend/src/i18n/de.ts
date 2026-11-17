import type { Strings } from './index'

export const de: Strings = {
  // the page
  'app.title': 'Ghostwriter · Chatassistent',

  // appheader
  'appheader.home': 'Übersicht',
  'appheader.theme_system': 'System',
  'appheader.theme_light': 'Hell',
  'appheader.theme_dark': 'Dunkel',
  'appheader.sign_out': 'Abmelden',
  'appheader.sidebar_toggle': 'Seitenleiste ein- oder ausblenden',

  // chat list
  'chat.list': 'Chats',
  'chat.list_empty': 'Noch keine Chats.',
  'chat.loading': 'Chats werden geladen…',
  'chat.new': 'Neuer Chat',
  'chat.untitled': 'Neuer Chat',
  'chat.options': 'Optionen für {title}',
  'chat.rename': 'Umbenennen',
  'chat.title': 'Chat-Titel',
  'chat.delete': 'Löschen',
  'chat.delete_confirm': 'Diesen Chat und seine Dateien löschen?',
  'chat.delete_busy': 'Beenden Sie die Antwort, bevor Sie den Chat löschen.',
  'chat.delete_files': 'Die Dateien des Chats konnten nicht gelöscht werden. Versuchen Sie es erneut.',

  // conversation
  'chat.empty': 'Fragen Sie, was Sie möchten, oder hängen Sie ein Dokument an.',
  'chat.empty_text_only': 'Fragen Sie, was Sie möchten.',
  'chat.loading_messages': 'Chat wird geladen…',
  'chat.answer_pending': 'Antwort wird erstellt…',
  'chat.thinking': 'denkt nach…',
  'chat.regenerate': 'Neu erzeugen',
  'chat.copy_answer': 'Antwort kopieren',
  'chat.copy_code': 'Code kopieren',
  'chat.answer_busy': 'In diesem Chat wird noch eine Antwort geschrieben.',
  'chat.no_answer': 'Es gibt keine Antwort, die neu erzeugt werden kann.',

  // the reasoning switch
  'chat.reasoning_state': 'Reasoning: {mode}',
  'chat.reasoning_on': 'An — das Modell denkt vor der Antwort nach',
  'chat.reasoning_off': 'Aus — schneller, direkte Antwort',

  // the composer
  'composer.placeholder': 'Nachricht schreiben',
  'composer.attach': 'Dateien anhängen',
  'composer.drop': 'Dateien zum Anhängen ablegen',
  'composer.files_stay': 'Angehängte Dateien bleiben in diesem Chat, bis er gelöscht wird, höchstens {days} Tage.',
  'composer.uploading': '{name} wird angehängt…',
  'composer.remove': '{name} entfernen',
  'composer.error_type': '{name}: Dieser Dateityp wird nicht unterstützt.',
  'composer.error_size': '{name} ist größer als 50 MB.',
  'composer.error_upload': '{name} konnte nicht angehängt werden.',
  'composer.error_storage_full':
    'Ihre Dateien füllen den von workbench erlaubten Platz; löschen Sie einen Chat oder warten Sie, bis Dateien ablaufen.',
  'composer.error_purpose':
    '{name} wurde nicht angehängt: Für das Anhängen von Dateien muss ein Zweck angegeben werden.',
  'composer.error_too_many_pending': 'Senden Sie zuerst die angehängten Dateien.',
  'composer.send': 'Senden',
  'composer.stop': 'Stopp',

  // dictation
  'mic.start': 'Diktieren',
  'mic.stop': 'Diktat beenden',
  'mic.denied': 'Der Zugriff auf das Mikrofon wurde verweigert.',
  'mic.unsupported': 'Dieser Browser kann kein Audio aufnehmen.',
  'mic.unavailable': 'Es konnte kein Mikrofon verwendet werden.',
  'mic.limit': 'noch {s} s',

  // the trail of tool steps
  'trail.steps': '{n} Schritte',
  'trail.steps_one': '1 Schritt',
  'trail.list_files': 'Dateien aufgelistet',
  'trail.read_document': '{detail} gelesen',
  'trail.read_document_unnamed': 'Ein Dokument gelesen',
  'trail.search_files': 'Nach {detail} gesucht',
  'trail.search_files_unnamed': 'Dateien durchsucht',
  'trail.run_python': 'Python ausgeführt',
  'trail.inspect_template': 'Vorlage {detail} geprüft',
  'trail.inspect_template_unnamed': 'Eine Vorlage geprüft',
  'trail.fill_template': 'Vorlage {detail} ausgefüllt',
  'trail.fill_template_unnamed': 'Eine Vorlage ausgefüllt',
  'trail.write_table': 'Tabelle {detail} geschrieben',
  'trail.write_table_unnamed': 'Eine Tabelle geschrieben',
  'trail.write_document': 'Dokument {detail} geschrieben',
  'trail.write_document_unnamed': 'Ein Dokument geschrieben',
  'trail.other': '{tool} verwendet',
  'trail.details': 'Details',
  'trail.expired': 'Details nach {days} Tagen gelöscht',
  'trail.status_running': 'Läuft',
  'trail.status_ok': 'Erledigt',
  'trail.status_error': 'Fehlgeschlagen',

  // result cards
  'result.more_rows': '{count} weitere Zeilen',
  'result.more_rows_one': '1 weitere Zeile',
  'result.more_rows_unknown': 'Weitere Zeilen in der Datei',
  'result.expired': 'Abgelaufen',
  'result.download': 'Herunterladen',
  'result.loading': '{name} wird geladen…',

  // why an answer ended early
  'error.model': 'Das Modell hat nicht geantwortet. Bitte erneut versuchen.',
  'error.audit': 'Die Antwort wurde abgebrochen, weil sie nicht protokolliert werden konnte.',
  'error.internal': 'Etwas ist schiefgelaufen.',

  // retention note
  'retention.chats': 'Chats werden {days} Tage nach ihrem Beginn gelöscht.',
  'retention.files': 'Dateien und Tool-Ergebnisse werden nach {days} Tagen gelöscht.',

  // purpose (PurposeBar; {max} is filled in by the bar)
  'purpose.label': 'Zweck',
  'purpose.empty': 'Kein Zweck angegeben',
  'purpose.required': 'Zweck erforderlich',
  'purpose.title': 'Zweck',
  'purpose.description':
    'Wird mit jeder Ihrer Anfragen in dieser App gesendet, bis Sie ihn ändern oder entfernen oder die Sitzung endet.',
  'purpose.requested': 'Für diese Aktion muss ein Zweck angegeben werden.',
  'purpose.case_ref': 'Aktenzeichen',
  'purpose.reason': 'Begründung',
  'purpose.save': 'Speichern',
  'purpose.cancel': 'Abbrechen',
  'purpose.clear': 'Entfernen',
  'purpose.error_length': 'Höchstens {max} Zeichen.',
  'purpose.error_control': 'Bitte entfernen Sie Zeilenumbrüche, Tabulatoren und andere Steuerzeichen.',
  'purpose.error_header': 'Zusammen zu lang zum Senden. Bitte kürzen Sie eines der Felder.',
  'purpose.error_empty': 'Bitte geben Sie ein Aktenzeichen oder eine Begründung an.',

  // common
  'common.cancel': 'Abbrechen',
  'common.copied': 'Kopiert',
  'common.error_request': 'Etwas ist schiefgelaufen ({status}) – Bitte versuchen Sie es erneut oder kontaktieren Sie den Support.',
  'common.error_unknown': 'Etwas ist schiefgelaufen – Bitte versuchen Sie es erneut oder kontaktieren Sie den Support.',
  'common.error_network': 'Dienst nicht erreichbar – Bitte prüfen Sie Ihre Verbindung oder versuchen Sie es später erneut.',
  'common.error_purpose_required':
    'Für diese Aktion muss ein Zweck angegeben werden – Bitte geben Sie ihn oben in der Kopfzeile an und versuchen Sie es erneut.',
}
