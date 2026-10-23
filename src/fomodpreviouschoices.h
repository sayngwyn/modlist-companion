#ifndef FOMODPREVIOUSCHOICES_H
#define FOMODPREVIOUSCHOICES_H

#include <QByteArray>
#include <QDateTime>
#include <QLatin1StringView>
#include <QString>
#include <QStringList>

#include <map>
#include <optional>
#include <tuple>

// Finds a mod's most recent recorded install and lets the installer dialog line its
// options up with that record. Qt Core only: no MO2 types, so it is testable alone.

namespace FomodLogger
{

/**
 * @brief Identity of one option across installs: (step name, group name, option
 * name, occurrence of that triple).
 *
 * Option ids are positional and only meaningful inside one event (older builds
 * numbered options in display order, newer ones in authoring order, and a mod
 * update can reorder them), so ids are never compared. The occurrence number keeps
 * repeated labels apart; it counts the triple through the whole event in recorded
 * order, exactly like python/fomod_logger_support/core/reviews.py::_option_paths.
 */
using OptionPath = std::tuple<QString, QString, QString, int>;

class OccurrenceCounter
{
public:
  /** @return the path for the next option carrying these names (0, 1, 2, ...). */
  OptionPath next(const QString& step, const QString& group, const QString& option);

private:
  std::map<std::tuple<QString, QString, QString>, int> m_Seen;
};

struct PreviousOption
{
  bool selected    = false;
  bool enabled     = true;  // false: forced or blocked by the FOMOD, not a choice
  bool stepVisible = true;  // false: the step was skipped, nothing was chosen
};

struct PreviousInstall
{
  QString eventId;
  QString instanceId;
  QString modName;
  QString archive;  // file name only
  int nexusId = 0;  // 0 when unknown
  QDateTime completedAt;  // for imported choices: when they were imported
  // True for choices read from an imported choices file rather than one of the user's
  // own installs. The installer only ever offers these; it never applies them by itself.
  bool imported = false;
  std::map<OptionPath, PreviousOption> options;

  const PreviousOption* find(const OptionPath& path) const;
};

/**
 * What to look for, in tiers; the first tier with any match decides, and the newest
 * record within that tier wins. Searching in tiers rather than pooling the names
 * matters: a patch hub file may be named after the mod it patches, and the newest
 * record across both names would then be the wrong mod's.
 */
struct PreviousQuery
{
  QString instanceId;
  /** Tier 1: the names this install will most likely be recorded under (the mod
   *  being reinstalled, the proposed name, the FOMOD's own name). Case-insensitive. */
  QStringList modNames;
  /** Tier 2: other guesses at the name (MO2's alternatives). Case-insensitive. */
  QStringList otherModNames;
  /** Tier 3: archive file name (folders are ignored); case-insensitive. */
  QString archiveFileName;
  /** Imported choices only, where it is the first tier: the mod's Nexus ID (0 or less
   *  when unknown, which matches nothing). */
  int nexusId = 0;
};

/** Largest event file that is read (real events are a few KB). */
constexpr qint64 kMaxEventFileBytes = 4LL * 1024 * 1024;
/** Most event files looked at in one scan, newest files first. */
constexpr int kMaxEventFiles = 5000;

/** Where the user's own installs are recorded, under the plugin data folder. */
constexpr QLatin1StringView kEventsFolder{"fomod_logger/events"};
/** Where imported choices are stored (see core/import_choices.py), under the same. */
constexpr QLatin1StringView kImportedFolder{"fomod_logger/imported"};
/** The "format" and "record_version" of an imported-choices record. */
constexpr QLatin1StringView kImportedRecordFormat{"fomod-companion-imported-choices"};
constexpr int kImportedRecordVersion = 1;

/**
 * @brief Parse one installation event. Returns nothing for anything that is not a
 * well-formed schema-1 event.
 */
std::optional<PreviousInstall> parsePreviousInstall(const QByteArray& json);

/**
 * @brief The newest recorded install (by completed_at) of the same MO2 instance
 * whose mod name matches the query's first tier of names, else its second tier, else
 * whose archive file name matches. Reads <pluginDataPath>/fomod_logger/events.
 * Unreadable, oversized or malformed files are skipped silently.
 */
std::optional<PreviousInstall> findPreviousInstall(const QString& pluginDataPath,
                                                   const PreviousQuery& query);

/**
 * @brief Parse one imported-choices record (written by python/fomod_logger_support/core/
 * import_choices.py). Returns nothing for anything that is not a well-formed version-1
 * record. The options are the file's picks; a skipped step counts as not shown.
 */
std::optional<PreviousInstall> parseImportedInstall(const QByteArray& json);

/**
 * @brief The imported choices to offer for a mod, when it has none of the user's own
 * (or they match nothing in the installer being shown; the caller decides).
 * Reads <pluginDataPath>/fomod_logger/imported for records of the same MO2 instance.
 *
 * A Nexus ID alone is not trusted: one Nexus page can hold several mods (a main file and
 * its patches), and a record can carry an ID that no longer fits. So a record with the
 * query's Nexus ID wins only when it agrees on the archive file name, else on the mod
 * name. After that the tiers are: archive file name, then mod name (the query's first,
 * then second tier of names), the newest import winning within a tier. A record that
 * matches by Nexus ID alone is used only when nothing else matched and no other record
 * shares that ID. Unreadable, oversized or malformed files are skipped silently.
 */
std::optional<PreviousInstall> findImportedInstall(const QString& pluginDataPath,
                                                   const PreviousQuery& query);

}  // namespace FomodLogger

#endif  // FOMODPREVIOUSCHOICES_H
