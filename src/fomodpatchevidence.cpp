#include "fomodpatchevidence.h"

#include <QDir>
#include <QFile>
#include <QHash>
#include <QJsonObject>
#include <QtEndian>

#include <algorithm>
#include <utility>

namespace
{

// Record header: type, data size, flags, form id, then (from Fallout 3 on) two
// version-control fields. Oblivion's header stops after the form id.
constexpr qint64 kOblivionHeaderSize = 20;
constexpr qint64 kRecordHeaderSize   = 24;
constexpr quint32 kCompressedFlag    = 0x00040000;
// Header data larger than this is not a plausible TES4 record.
constexpr quint32 kMaxHeaderDataSize = 4 * 1024 * 1024;

QJsonArray toArray(const QStringList& values)
{
  QJsonArray array;
  for (const QString& value : values) {
    array.append(value);
  }
  return array;
}

QJsonArray toArray(const QList<FomodLogger::FileRecord>& records)
{
  QJsonArray array;
  for (const FomodLogger::FileRecord& record : records) {
    QJsonObject entry;
    entry.insert("source", record.source);
    entry.insert("destination", record.destination);
    entry.insert("resolved", record.resolved);
    if (record.escapes) {
      entry.insert("escapes", true);
    }
    entry.insert("kind", record.isFolder ? QStringLiteral("folder")
                                         : QStringLiteral("file"));
    if (record.priority != 0) {
      entry.insert("priority", record.priority);
    }
    entry.insert("order", record.order);
    array.append(entry);
  }
  return array;
}

// The recorded file entries of one item: `files` always (an empty list says "recorded,
// installs nothing"), `files_truncated` only when the list was cut.
void insertFiles(QJsonObject& object, const FomodLogger::PatchEvidence& item)
{
  if (!item.hasFiles) {
    return;
  }
  object.insert("files", toArray(item.files));
  if (item.filesTruncated) {
    object.insert("files_truncated", true);
  }
}

// Bytes this entry takes in the event JSON, estimated: its strings (a backslash or a
// quote takes two) plus the indented JSON around them.
qint64 entryBytes(const FomodLogger::FileRecord& record)
{
  qint64 bytes = FomodLogger::kFileEntryOverheadBytes;
  for (const QString* text : {&record.source, &record.destination, &record.resolved}) {
    bytes += text->toUtf8().size();
    bytes += text->count(QLatin1Char('\\')) + text->count(QLatin1Char('"'));
  }
  return bytes;
}

// Keep at most maxCount entries of at most maxBytes, preferring the ones that win (highest
// priority, then latest in the XML); the survivors stay in their original order.
// Returns the bytes kept.
qint64 trimToBudget(FomodLogger::PatchEvidence& item, qsizetype maxCount, qint64 maxBytes)
{
  qint64 total = 0;
  QList<qint64> sizes;
  sizes.reserve(item.files.size());
  for (const FomodLogger::FileRecord& record : std::as_const(item.files)) {
    sizes.append(entryBytes(record));
    total += sizes.last();
  }
  if (item.files.size() <= maxCount && total <= maxBytes) {
    return total;
  }

  QList<qsizetype> byRank;
  byRank.reserve(item.files.size());
  for (qsizetype i = 0; i < item.files.size(); ++i) {
    byRank.append(i);
  }
  std::sort(byRank.begin(), byRank.end(), [&item](qsizetype lhs, qsizetype rhs) {
    const FomodLogger::FileRecord& a = item.files[lhs];
    const FomodLogger::FileRecord& b = item.files[rhs];
    return a.priority != b.priority ? a.priority > b.priority : a.order > b.order;
  });
  QList<bool> keep(item.files.size(), false);
  qsizetype kept  = 0;
  qint64 keptBytes = 0;
  for (const qsizetype index : std::as_const(byRank)) {
    if (kept >= maxCount) {
      break;
    }
    if (keptBytes + sizes[index] <= maxBytes) {
      keep[index] = true;
      ++kept;
      keptBytes += sizes[index];
    }
  }
  QList<FomodLogger::FileRecord> survivors;
  survivors.reserve(kept);
  for (qsizetype i = 0; i < item.files.size(); ++i) {
    if (keep[i]) {
      survivors.append(item.files[i]);
    }
  }
  item.files          = survivors;
  item.filesTruncated = true;
  return keptBytes;
}

}  // namespace

namespace FomodLogger
{

QString resolveFileDestination(const QString& source, const QString& destination,
                               bool isFolder, bool* escapes)
{
  if (escapes != nullptr) {
    *escapes = false;
  }
  QString path = destination;
  path.replace(QLatin1Char('\\'), QLatin1Char('/'));
  QString name = source;
  name.replace(QLatin1Char('\\'), QLatin1Char('/'));
  name = name.mid(name.lastIndexOf('/') + 1);

  // Outside the mod folder: a drive letter or a UNC path. (".." is checked once tidied.)
  const bool drive = path.size() >= 2 && path[0].isLetter() && path[1] == QLatin1Char(':');
  bool outside     = drive || path.startsWith(QLatin1String("//"));
  const bool wantsName = !isFolder && (path.isEmpty() || path.endsWith('/'));

  path = QDir::cleanPath(path);
  // A leading slash names the mod folder, so it goes before ".." is looked for: "/../x" must
  // not slip through as "../x" (cleanPath keeps a ".." that is right after the root).
  while (path.startsWith('/')) {
    path.remove(0, 1);
  }
  if (path == QLatin1String("..") || path.startsWith(QLatin1String("../"))) {
    outside = true;
  }
  if (outside) {
    if (escapes != nullptr) {
      *escapes = true;
    }
    return QString();
  }
  // The mod folder is the game's Data folder, so a leading "Data/" names nothing extra.
  if (path.compare(QLatin1String("data"), Qt::CaseInsensitive) == 0) {
    path.clear();
  } else if (path.startsWith(QLatin1String("data/"), Qt::CaseInsensitive)) {
    path.remove(0, 5);
  }
  if (path == QLatin1String(".")) {
    path.clear();
  }
  // A file with no destination name, or one that names the mod folder, keeps its own name.
  if (!isFolder && (wantsName || path.isEmpty())) {
    path = path.isEmpty() ? name : path + QLatin1Char('/') + name;
  }
  return path;
}

void capFileRecords(QList<PatchEvidence>& evidence, qint64 maxBytes)
{
  qsizetype remainingCount = kMaxFilesPerEvent;
  qint64 remainingBytes    = maxBytes;
  // The budget goes to the required files first, then to what is installed (picked
  // options, automatic installs that applied), then to the options that were skipped.
  for (int pass = 0; pass < 3; ++pass) {
    for (PatchEvidence& item : evidence) {
      if (!item.hasFiles) {
        continue;
      }
      const int itemPass = item.id == kRequiredFilesId ? 0 : (item.installed ? 1 : 2);
      if (itemPass != pass) {
        continue;
      }
      const qsizetype allowed = (std::min)(kMaxFilesPerOption, remainingCount);
      const qint64 used       = trimToBudget(item, allowed, remainingBytes);
      remainingCount -= item.files.size();
      remainingBytes -= used;
    }
  }
}

std::optional<QStringList> readPluginMasters(const QString& path)
{
  QFile file(path);
  if (!file.open(QIODevice::ReadOnly)) {
    return std::nullopt;
  }
  // Read enough for the longer header plus a subrecord type: a TES4 record's data
  // begins with HEDR, which tells the two header layouts apart.
  const QByteArray header = file.read(kRecordHeaderSize + 4);
  if (header.size() < kOblivionHeaderSize + 4 || !header.startsWith("TES4")) {
    return std::nullopt;
  }
  qint64 headerSize = 0;
  if (header.mid(kOblivionHeaderSize, 4) == "HEDR") {
    headerSize = kOblivionHeaderSize;
  } else if (header.size() == kRecordHeaderSize + 4 &&
             header.mid(kRecordHeaderSize, 4) == "HEDR") {
    headerSize = kRecordHeaderSize;
  } else {
    return std::nullopt;
  }
  const quint32 dataSize = qFromLittleEndian<quint32>(header.constData() + 4);
  const quint32 flags    = qFromLittleEndian<quint32>(header.constData() + 8);
  if (dataSize > kMaxHeaderDataSize || (flags & kCompressedFlag) != 0 ||
      !file.seek(headerSize)) {
    return std::nullopt;
  }
  const QByteArray data = file.read(dataSize);
  if (data.size() != static_cast<qsizetype>(dataSize)) {
    return std::nullopt;
  }

  QStringList masters;
  qsizetype offset     = 0;
  quint32 extendedSize = 0;
  while (offset + 6 <= data.size()) {
    const QByteArray type = data.mid(offset, 4);
    quint32 size          = qFromLittleEndian<quint16>(data.constData() + offset + 4);
    offset += 6;
    if (extendedSize != 0) {
      size         = extendedSize;
      extendedSize = 0;
    }
    if (offset + static_cast<qsizetype>(size) > data.size()) {
      return std::nullopt;
    }
    if (type == "XXXX" && size == 4) {
      extendedSize = qFromLittleEndian<quint32>(data.constData() + offset);
    } else if (type == "MAST" && size > 0) {
      QByteArray name = data.mid(offset, size);
      const qsizetype end = name.indexOf('\0');
      if (end >= 0) {
        name.truncate(end);
      }
      // Plugin names are Windows-1252 in practice; Latin-1 keeps ASCII names exact.
      const QString master = QString::fromLatin1(name).trimmed();
      if (!master.isEmpty() && !masters.contains(master, Qt::CaseInsensitive)) {
        masters.append(master);
      }
    }
    offset += size;
  }
  return masters;
}

QJsonArray attachPatchEvidence(const QJsonArray& steps,
                               const QList<PatchEvidence>& evidence)
{
  QHash<QString, const PatchEvidence*> byId;
  for (const PatchEvidence& item : evidence) {
    byId.insert(item.id, &item);
  }

  QJsonArray result;
  for (const QJsonValue& stepValue : steps) {
    QJsonObject step = stepValue.toObject();
    QJsonArray groups;
    for (const QJsonValue& groupValue : step.value("groups").toArray()) {
      QJsonObject group = groupValue.toObject();
      QJsonArray options;
      for (const QJsonValue& optionValue : group.value("options").toArray()) {
        QJsonObject option = optionValue.toObject();
        if (const PatchEvidence* item = byId.value(option.value("id").toString())) {
          option.insert("plugins", toArray(item->plugins));
          if (item->mastersRead) {
            option.insert("masters", toArray(item->masters));
          }
          option.insert("detects", toArray(item->detects));
          insertFiles(option, *item);
        }
        options.append(option);
      }
      group.insert("options", options);
      groups.append(group);
    }
    step.insert("groups", groups);
    result.append(step);
  }
  return result;
}

QJsonObject requiredFiles(const QList<PatchEvidence>& evidence)
{
  for (const PatchEvidence& item : evidence) {
    if (item.id == kRequiredFilesId && item.hasFiles) {
      QJsonObject result;
      insertFiles(result, item);
      return result;
    }
  }
  return {};
}

QJsonArray automaticInstalls(const QList<PatchEvidence>& evidence)
{
  QJsonArray result;
  for (const PatchEvidence& item : evidence) {
    if (!item.id.startsWith(QStringLiteral("conditional:"))) {
      continue;
    }
    QJsonObject entry;
    entry.insert("id", item.id);
    entry.insert("installed", item.installed);
    entry.insert("plugins", toArray(item.plugins));
    if (item.mastersRead) {
      entry.insert("masters", toArray(item.masters));
    }
    entry.insert("detects", toArray(item.detects));
    insertFiles(entry, item);
    result.append(entry);
  }
  return result;
}

}  // namespace FomodLogger
