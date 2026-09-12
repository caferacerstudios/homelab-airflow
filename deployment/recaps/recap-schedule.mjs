// Source phase and state interpretation shared by the original website contract.
const text = (value) => String(value ?? "").trim();
const integer = (value) => Number.isInteger(Number(value)) ? Number(value) : null;
export function schedulePhase(game) {
  const value = text(game?.phase ?? game?.season_type ?? game?.seasonType ?? game?.type).toLowerCase();
  if (value.includes("pre")) return "preseason";
  if (value.includes("post") || value.includes("playoff") || game?.postseason === true || game?.is_postseason === true) return "postseason";
  if (value.includes("regular")) return "regular";
  if (!value && (game?.postseason === false || game?.is_postseason === false)) {
    const season = integer(game?.season);
    const kickoffValue = sourceDate(game);
    const kickoff = kickoffValue ? new Date(kickoffValue) : null;
    if (season && kickoff && Number.isFinite(kickoff.getTime())) {
      const septemberFirst = new Date(Date.UTC(season, 8, 1));
      const laborDay = 1 + ((8 - septemberFirst.getUTCDay()) % 7);
      const regularSeasonOpener = Date.UTC(season, 8, laborDay + 3);
      return kickoff.getTime() < regularSeasonOpener ? "preseason" : "regular";
    }
  }
  return null;
}

export function scheduleState(game) {
  if (game?.bye === true || text(game?.kind).toLowerCase() === "bye" || /\bbye\b/i.test(text(game?.status))) return "bye";
  const value = text(game?.state ?? game?.status).toLowerCase().replaceAll("-", "_").replaceAll(" ", "_");
  if (value.includes("cancel")) return "canceled";
  if (value.includes("postpon")) return "postponed";
  if (/^(?:final(?:$|[_/\(])|finished$|completed?$|closed$)/.test(value)) return "completed";
  if (/in_progress|live|halftime|quarter|overtime/.test(value)) return "in_progress";
  if (/tbd|to_be_determined|unconfirmed/.test(value)) return "tbd";
  return "upcoming";
}

function sourceDate(game) {
  return game?.startsAt ?? game?.datetime ?? game?.start_time ?? game?.kickoff ?? game?.date ?? null;
}
