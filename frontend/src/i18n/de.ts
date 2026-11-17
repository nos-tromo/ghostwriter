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
  'chat.delete_busy': 'Beende die Antwort, bevor du den Chat löschst.',

  // retention banner
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
