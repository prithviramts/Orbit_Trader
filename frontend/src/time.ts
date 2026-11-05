/** Date helpers shared by the ticker and charts (no chart library import, so the app shell stays small). */
export function nyDay(sec: number) {
  return new Date(sec * 1000).toLocaleDateString("en-CA", { timeZone: "America/New_York" });
}

export function dayToTs(day: string) {
  return Math.floor(Date.UTC(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10)) / 1000);
}
