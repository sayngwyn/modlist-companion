#ifndef FOMODPATCHEVIDENCE_H
#define FOMODPATCHEVIDENCE_H

#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QString>
#include <QStringList>

#include <optional>

namespace FomodLogger
{

/**
 * @brief Read the master list from a Bethesda plugin header (TES4 record).
 *
 * Only the header record is read, so this works on any size of plugin. Handles the
 * 20-byte record header of Oblivion and the 24-byte one of later games. Returns
 * std::nullopt ("not read") for anything that is not a readable TES4 record, so a
 * failed read is never mistaken for "no masters".
 */
std::optional<QStringList> readPluginMasters(const QString& path);

/**
 * @brief One <file> or <folder> entry of a FOMOD, as ModuleConfig.xml defines it.
 *
 * This is the XML definition, not the extracted file list, so it stays small even for
 * huge texture packs.
 */
struct FileRecord
{
  QString source;       // as written in the XML
  QString destination;  // as written in the XML
  // Where it lands: forward slashes, relative to the mod folder (a leading "Data/" is
  // dropped), case kept. Empty means the mod folder itself, and only for a folder; it is
  // also empty when `escapes` is set.
  QString resolved;
  bool isFolder = false;
  // The destination points outside the mod folder ("..", a drive letter, a UNC path).
  bool escapes = false;
  int priority = 0;
  int order    = 0;  // position of the entry in ModuleConfig.xml (breaks priority ties)
};

// At most this many entries are recorded for one option, and for one whole event.
constexpr qsizetype kMaxFilesPerOption = 2000;
constexpr qsizetype kMaxFilesPerEvent  = 20000;
// The recorded entries of one event stay under this many bytes of event JSON (estimated
// from the text lengths plus kFileEntryOverheadBytes each), so the whole event stays well
// under the 4 MiB the previous-choices reader accepts.
constexpr qint64 kMaxFileBytesPerEvent  = 2LL * 1024 * 1024;
constexpr qint64 kFileEntryOverheadBytes = 360;  // indented JSON around the strings

/**
 * @brief Where a <file>/<folder> entry installs, the way the FOMOD installer places it:
 *     a file with no destination, one ending in a slash, or one that names the mod folder
 *     keeps its own name. A leading "Data/" is dropped. A destination outside the mod
 *     folder ("..", a drive letter, a UNC path) resolves to "" with *escapes set.
 */
QString resolveFileDestination(const QString& source, const QString& destination,
                               bool isFolder, bool* escapes = nullptr);

/**
 * @brief Patch evidence for one option or one automatic (conditional) install.
 */
struct PatchEvidence
{
  QString id;
  QStringList plugins;  // plugin file names the option would install
  QStringList masters;  // masters of those plugins, when they could be read
  QStringList detects;  // plugins the FOMOD's own conditions look for
  bool installed   = false;
  bool mastersRead = false;  // false when the plugins were not extracted
  QList<FileRecord> files;
  bool hasFiles       = false;  // false: file entries were not recorded for this item
  bool filesTruncated = false;  // the list was cut at a cap
};

// The id of the evidence item that holds the FOMOD's requiredInstallFiles.
inline const QString kRequiredFilesId = QStringLiteral("required");

/**
 * @brief Apply the per-option, per-event and byte caps to the recorded file entries. The
 *     budget goes first to the required files, then to the options that were picked and
 *     the automatic installs that applied, then to the rest. An item that does not fit
 *     keeps its entries of highest (priority, order), the ones that win, in their
 *     original order, and is marked filesTruncated.
 */
void capFileRecords(QList<PatchEvidence>& evidence,
                    qint64 maxBytes = kMaxFileBytesPerEvent);

/**
 * @brief Add plugins/masters/detects (and files, when recorded) to matching options in a
 *     selection snapshot.
 */
QJsonArray attachPatchEvidence(const QJsonArray& steps,
                               const QList<PatchEvidence>& evidence);

/**
 * @brief Automatic (conditional) installs as event JSON, skipping option entries.
 */
QJsonArray automaticInstalls(const QList<PatchEvidence>& evidence);

/**
 * @brief The FOMOD's requiredInstallFiles as event JSON ({files, files_truncated?}), or
 *     an empty object when there are none.
 */
QJsonObject requiredFiles(const QList<PatchEvidence>& evidence);

}  // namespace FomodLogger

#endif  // FOMODPATCHEVIDENCE_H
