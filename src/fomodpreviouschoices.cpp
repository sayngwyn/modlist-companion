#include "fomodpreviouschoices.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

#include <algorithm>
#include <vector>

namespace FomodLogger
{

OptionPath OccurrenceCounter::next(const QString& step, const QString& group,
                                   const QString& option)
{
  const auto key = std::make_tuple(step, group, option);
  const auto it  = m_Seen.find(key);
  const int n    = it == m_Seen.end() ? 0 : it->second + 1;
  m_Seen[key]    = n;
  return OptionPath(step, group, option, n);
}

const PreviousOption* PreviousInstall::find(const OptionPath& path) const
{
  const auto it = options.find(path);
  return it == options.end() ? nullptr : &it->second;
}

namespace
{

QDateTime parseTime(const QString& text)
{
  QDateTime value = QDateTime::fromString(text, Qt::ISODateWithMs);
  if (!value.isValid()) {
    value = QDateTime::fromString(text, Qt::ISODate);
  }
  return value;
}

QString fileNameOf(QString path)
{
  path.replace(QLatin1Char('\\'), QLatin1Char('/'));
  return path.mid(path.lastIndexOf(QLatin1Char('/')) + 1);
}

// Everything except the options, which are only read for the event that is kept.
bool readHeader(const QJsonObject& event, PreviousInstall& out)
{
  if (event.value(QStringLiteral("schema_version")).toInt(0) != 1) {
    return false;
  }
  const QJsonValue steps = event.value(QStringLiteral("steps"));
  if (!steps.isArray()) {
    return false;
  }
  out.eventId = event.value(QStringLiteral("event_id")).toString();
  out.completedAt =
      parseTime(event.value(QStringLiteral("completed_at")).toString());
  out.instanceId =
      event.value(QStringLiteral("instance")).toObject().value(QStringLiteral("id")).toString();
  out.modName =
      event.value(QStringLiteral("mod")).toObject().value(QStringLiteral("name")).toString();
  out.archive = fileNameOf(event.value(QStringLiteral("source"))
                               .toObject()
                               .value(QStringLiteral("archive"))
                               .toString());
  return !out.eventId.isEmpty() && out.completedAt.isValid() &&
         !out.instanceId.isEmpty();
}

// The options of an install event, or (out.imported) of an imported entry, whose steps say
// "skipped" where an event says "visible" and whose options say "picked" where an event
// says "selected".
void readSteps(const QJsonArray& steps, PreviousInstall& out)
{
  const bool imported = out.imported;
  OccurrenceCounter counter;
  for (const QJsonValue& stepValue : steps) {
    const QJsonObject step = stepValue.toObject();
    const QString stepName = step.value(QStringLiteral("name")).toString();
    const bool visible =
        imported ? !step.value(QStringLiteral("skipped")).toBool(false)
                 : step.value(QStringLiteral("visible")).toBool(true);
    for (const QJsonValue& groupValue : step.value(QStringLiteral("groups")).toArray()) {
      const QJsonObject group = groupValue.toObject();
      const QString groupName = group.value(QStringLiteral("name")).toString();
      for (const QJsonValue& optionValue :
           group.value(QStringLiteral("options")).toArray()) {
        const QJsonObject option = optionValue.toObject();
        // Counted even for skipped steps: the occurrence is a position in the event.
        const OptionPath path = counter.next(
            stepName, groupName, option.value(QStringLiteral("name")).toString());
        PreviousOption entry;
        entry.selected    = option.value(imported ? QStringLiteral("picked")
                                                  : QStringLiteral("selected"))
                                .toBool(false);
        entry.enabled     = option.value(QStringLiteral("enabled")).toBool(true);
        entry.stepVisible = visible;
        out.options.emplace(path, entry);
      }
    }
  }
}

void readOptions(const QJsonObject& event, PreviousInstall& out)
{
  readSteps(event.value(QStringLiteral("steps")).toArray(), out);
}

bool isNewer(const PreviousInstall& candidate, const PreviousInstall& best)
{
  if (candidate.completedAt != best.completedAt) {
    return candidate.completedAt > best.completedAt;
  }
  return candidate.eventId > best.eventId;
}

// The header of an imported record (the mod's identity); the options are read separately.
bool readImportedHeader(const QJsonObject& record, PreviousInstall& out)
{
  if (record.value(QStringLiteral("format")).toString() != kImportedRecordFormat ||
      record.value(QStringLiteral("record_version")).toInt(0) != kImportedRecordVersion) {
    return false;
  }
  const QJsonObject entry = record.value(QStringLiteral("entry")).toObject();
  if (!entry.value(QStringLiteral("steps")).isArray()) {
    return false;
  }
  out.imported   = true;
  out.instanceId = record.value(QStringLiteral("instance_id")).toString();
  out.completedAt =
      parseTime(record.value(QStringLiteral("imported_at")).toString());
  out.modName = entry.value(QStringLiteral("name")).toString();
  out.archive = fileNameOf(entry.value(QStringLiteral("archive")).toString());
  out.nexusId = entry.value(QStringLiteral("nexus_id")).toInt(0);
  return !out.modName.trimmed().isEmpty() && out.completedAt.isValid() &&
         !out.instanceId.isEmpty();
}

void readImportedOptions(const QJsonObject& record, PreviousInstall& out)
{
  readSteps(record.value(QStringLiteral("entry"))
                .toObject()
                .value(QStringLiteral("steps"))
                .toArray(),
            out);
}

// The records of a folder to look at: readable *.json files; when there are too many, the
// newest (ties broken by file name, so the same files are kept every time).
QFileInfoList recordFiles(const QDir& directory)
{
  QFileInfoList files = directory.entryInfoList(
      {QStringLiteral("*.json")}, QDir::Files | QDir::Readable, QDir::NoSort);
  if (files.size() > kMaxEventFiles) {
    std::sort(files.begin(), files.end(), [](const QFileInfo& a, const QFileInfo& b) {
      if (a.lastModified() != b.lastModified()) {
        return a.lastModified() > b.lastModified();
      }
      return a.fileName().compare(b.fileName(), Qt::CaseInsensitive) < 0;
    });
    files.resize(kMaxEventFiles);
  }
  return files;
}

// A file's whole contents when it is a plausible record, else nothing.
QByteArray readRecordBytes(const QFileInfo& info)
{
  if (info.size() <= 0 || info.size() > kMaxEventFileBytes) {
    return {};
  }
  QFile file(info.absoluteFilePath());
  if (!file.open(QIODevice::ReadOnly)) {
    return {};
  }
  const QByteArray bytes = file.read(kMaxEventFileBytes + 1);
  return bytes.size() > kMaxEventFileBytes ? QByteArray() : bytes;
}

QStringList cleanedNames(const QStringList& list)
{
  QStringList names;
  for (const QString& name : list) {
    const QString trimmed = name.trimmed();
    if (!trimmed.isEmpty() && !names.contains(trimmed, Qt::CaseInsensitive)) {
      names.append(trimmed);
    }
  }
  return names;
}

bool hasName(const QStringList& names, const QString& modName)
{
  const QString trimmed = modName.trimmed();
  return !trimmed.isEmpty() &&
         std::any_of(names.begin(), names.end(), [&](const QString& n) {
           return n.compare(trimmed, Qt::CaseInsensitive) == 0;
         });
}

}  // namespace

std::optional<PreviousInstall> parsePreviousInstall(const QByteArray& json)
{
  QJsonParseError error;
  const QJsonDocument document = QJsonDocument::fromJson(json, &error);
  if (error.error != QJsonParseError::NoError || !document.isObject()) {
    return std::nullopt;
  }
  PreviousInstall install;
  if (!readHeader(document.object(), install)) {
    return std::nullopt;
  }
  readOptions(document.object(), install);
  return install;
}

std::optional<PreviousInstall> findPreviousInstall(const QString& pluginDataPath,
                                                   const PreviousQuery& query)
{
  if (pluginDataPath.isEmpty() || query.instanceId.isEmpty()) {
    return std::nullopt;
  }
  const QDir directory(QDir(pluginDataPath).filePath(kEventsFolder));
  if (!directory.exists()) {
    return std::nullopt;
  }

  const QStringList primaryNames = cleanedNames(query.modNames);
  const QStringList otherNames   = cleanedNames(query.otherModNames);
  const QString archive      = fileNameOf(query.archiveFileName);
  const QByteArray instanceBytes = query.instanceId.toUtf8();

  const QFileInfoList files = recordFiles(directory);

  // Best record per tier; one record can be the best of several tiers.
  std::optional<PreviousInstall> byPrimary;
  std::optional<PreviousInstall> byOther;
  std::optional<PreviousInstall> byArchive;

  for (const QFileInfo& info : files) {
    const QByteArray bytes = readRecordBytes(info);
    if (bytes.isEmpty()) {
      continue;
    }
    // Cheap rejection of other MO2 instances before any JSON work.
    if (!bytes.contains(instanceBytes)) {
      continue;
    }

    QJsonParseError error;
    const QJsonDocument document = QJsonDocument::fromJson(bytes, &error);
    if (error.error != QJsonParseError::NoError || !document.isObject()) {
      continue;
    }
    const QJsonObject event = document.object();
    PreviousInstall header;
    if (!readHeader(event, header) || header.instanceId != query.instanceId) {
      continue;
    }

    const bool primaryMatch = hasName(primaryNames, header.modName);
    const bool otherMatch   = hasName(otherNames, header.modName);
    const bool archiveMatch = !archive.isEmpty() && !header.archive.isEmpty() &&
                              archive.compare(header.archive, Qt::CaseInsensitive) == 0;

    std::vector<std::optional<PreviousInstall>*> targets;
    const auto consider = [&](bool matches, std::optional<PreviousInstall>& target) {
      if (matches && (!target || isNewer(header, *target))) {
        targets.push_back(&target);
      }
    };
    consider(primaryMatch, byPrimary);
    consider(otherMatch, byOther);
    consider(archiveMatch, byArchive);
    if (targets.empty()) {
      continue;
    }
    readOptions(event, header);
    for (std::optional<PreviousInstall>* target : targets) {
      *target = header;
    }
  }

  if (byPrimary) {
    return byPrimary;
  }
  return byOther ? byOther : byArchive;
}

std::optional<PreviousInstall> parseImportedInstall(const QByteArray& json)
{
  QJsonParseError error;
  const QJsonDocument document = QJsonDocument::fromJson(json, &error);
  if (error.error != QJsonParseError::NoError || !document.isObject()) {
    return std::nullopt;
  }
  PreviousInstall install;
  if (!readImportedHeader(document.object(), install)) {
    return std::nullopt;
  }
  readImportedOptions(document.object(), install);
  return install;
}

std::optional<PreviousInstall> findImportedInstall(const QString& pluginDataPath,
                                                   const PreviousQuery& query)
{
  if (pluginDataPath.isEmpty() || query.instanceId.isEmpty()) {
    return std::nullopt;
  }
  const QDir directory(QDir(pluginDataPath).filePath(kImportedFolder));
  if (!directory.exists()) {
    return std::nullopt;
  }

  const QStringList primaryNames = cleanedNames(query.modNames);
  const QStringList otherNames   = cleanedNames(query.otherModNames);
  const QString archive          = fileNameOf(query.archiveFileName);
  if (query.nexusId <= 0 && archive.isEmpty() && primaryNames.isEmpty() &&
      otherNames.isEmpty()) {
    return std::nullopt;  // nothing to look this mod up by
  }
  const QByteArray instanceBytes = query.instanceId.toUtf8();

  // A record's identity only; the options of the one that is chosen are read at the end.
  struct Candidate
  {
    PreviousInstall install;
    QString path;
    QString file;  // tie-break between records imported in the same second
  };
  const auto archiveMatches = [&](const PreviousInstall& found) {
    return !archive.isEmpty() && !found.archive.isEmpty() &&
           archive.compare(found.archive, Qt::CaseInsensitive) == 0;
  };
  const auto nameMatches = [&](const PreviousInstall& found) {
    return hasName(primaryNames, found.modName) || hasName(otherNames, found.modName);
  };
  const auto newer = [](const Candidate& a, const Candidate& b) {
    return a.install.completedAt != b.install.completedAt
               ? a.install.completedAt > b.install.completedAt
               : a.file.compare(b.file, Qt::CaseInsensitive) > 0;
  };
  const auto keepNewest = [&](std::optional<Candidate>& slot, const Candidate& candidate) {
    if (!slot || newer(candidate, *slot)) {
      slot = candidate;
    }
  };

  std::vector<Candidate> byNexus;  // every record with the query's Nexus ID
  std::optional<Candidate> byArchive;
  std::optional<Candidate> byPrimary;
  std::optional<Candidate> byOther;
  for (const QFileInfo& info : recordFiles(directory)) {
    const QByteArray bytes = readRecordBytes(info);
    if (bytes.isEmpty() || !bytes.contains(instanceBytes)) {
      continue;
    }
    QJsonParseError error;
    const QJsonDocument document = QJsonDocument::fromJson(bytes, &error);
    if (error.error != QJsonParseError::NoError || !document.isObject()) {
      continue;
    }
    Candidate candidate;
    if (!readImportedHeader(document.object(), candidate.install) ||
        candidate.install.instanceId != query.instanceId) {
      continue;
    }
    candidate.path = info.absoluteFilePath();
    candidate.file = info.fileName();
    const PreviousInstall& found = candidate.install;
    if (query.nexusId > 0 && found.nexusId == query.nexusId) {
      byNexus.push_back(candidate);
    }
    if (archiveMatches(found)) {
      keepNewest(byArchive, candidate);
    }
    if (hasName(primaryNames, found.modName)) {
      keepNewest(byPrimary, candidate);
    }
    if (hasName(otherNames, found.modName)) {
      keepNewest(byOther, candidate);
    }
  }

  // The records to try, best first. A Nexus ID is only believed together with the archive
  // or, failing that, the name; alone it is the last resort.
  std::optional<Candidate> agreed;
  for (const Candidate& candidate : byNexus) {
    if (archiveMatches(candidate.install)) {
      keepNewest(agreed, candidate);
    }
  }
  if (!agreed) {
    for (const Candidate& candidate : byNexus) {
      if (nameMatches(candidate.install)) {
        keepNewest(agreed, candidate);
      }
    }
  }
  std::vector<Candidate> ranked;
  const auto add = [&](const Candidate& candidate) {
    if (std::none_of(ranked.begin(), ranked.end(),
                     [&](const Candidate& c) { return c.path == candidate.path; })) {
      ranked.push_back(candidate);
    }
  };
  for (const std::optional<Candidate>* tier : {&agreed, &byArchive, &byPrimary, &byOther}) {
    if (*tier) {
      add(**tier);
    }
  }
  if (byNexus.size() == 1) {
    add(byNexus.front());  // the only record with this ID, and nothing else matched
  }

  // Read the options of the first that still checks out: a file can change between the scan
  // and now.
  for (const Candidate& candidate : ranked) {
    std::optional<PreviousInstall> result =
        parseImportedInstall(readRecordBytes(QFileInfo(candidate.path)));
    if (result && result->instanceId == query.instanceId &&
        result->modName == candidate.install.modName) {
      result->eventId = candidate.file;
      return result;
    }
  }
  return std::nullopt;
}

}  // namespace FomodLogger
