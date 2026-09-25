export const displayColors = ['neutral', 'violet', 'blue', 'cyan', 'green', 'yellow', 'orange', 'red', 'pink']

export function displayColor(value) {
  return displayColors.includes(value) ? value : 'neutral'
}

export function nameHistoryTime(value) {
  const date = new Date(value)
  return value && !Number.isNaN(date.getTime())
    ? new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date)
    : 'Date unavailable'
}
