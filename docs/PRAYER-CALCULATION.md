# Offline prayer calculation

**Integration note.** This document came from the second agent's branch, where
it described a standalone `prayer/calculator.py` adapter. That adapter is not
what shipped: its argument — that adhanpy is a better bet than hand-rolled
trigonometry — was accepted, and adhanpy now sits behind the existing
`prayer.times.compute()` instead, so the tool, the reminder rule and fifty-one
tests carried over unchanged. Its handling of high latitudes was taken too, and
is why a time produced by a convention now says so.

The comparison that decided it: across five cities and four solstice and equinox
dates, this project's own trigonometry and adhanpy agreed to within three
minutes everywhere and within one at Almaty.

---

`atlas_backend.prayer.calculator` extends the earlier imported timetable module
without changing its five-prayer JSON contract. The calculator returns **Fajr,
Sunrise, Dhuhr, Asr, Maghrib and Isha**. Sunrise can be queried explicitly but is
excluded from the default "next prayer" selection.

The owner must explicitly choose coordinates, IANA timezone, calculation method,
Asr method and high-latitude rule. Supported methods are Muslim World League,
Karachi, Egyptian and North America; Asr is standard or Hanafi. These choices
are not a recommendation for a particular locality. No AI, account, network
request, automatic geolocation or paid service is involved in calculation.

The engine is the MIT-licensed [adhanpy 1.0.5](https://github.com/alphahm/adhanpy),
pinned in the lockfile, with no transitive runtime dependencies. Its numerical
reference vector for Raleigh on 2015-07-12 is tested independently of adapter
output. Coordinates are never included in error messages or model-facing
results. Every result retains the actual method, Asr rule, timezone and source.

Civil-date alignment handles the international date line. Fajr and Isha may
fall on adjacent civil dates; next-prayer lookup examines adjoining calculated
schedules using UTC comparisons. Unsupported polar calculations fail clearly,
without fabricated or nearest-location fallback. Supported dates are
1970-01-02 through 2099-12-30, with next-event queries requiring adjacent dates
to remain in that range. Countdown values round up fractional seconds.

Calculated times should be compared with the owner's accepted local timetable
before relying on automatic reminders. Importing an accepted timetable remains
an alternative when local adjustments differ. No convention has been chosen
or activated for the owner by development tests.
