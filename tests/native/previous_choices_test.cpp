// Qt-only test of src/fomodpreviouschoices.cpp, src/fomodpatchevidence.cpp and
// src/fomodloggereventwriter.cpp (no MO2 needed).
//
// Build with -DFOMOD_LOGGER_BUILD_NATIVE_TESTS=ON (target previous_choices_test) and
// run the executable; it exits 0 when everything passes. With a second argument (a
// folder) it also writes a sample event with recorded files there, for the Python
// tests to check against the schema.

#include "fomodloggereventwriter.h"
#include "fomodpatchevidence.h"
#include "fomodpreviouschoices.h"

#include <QByteArray>
#include <QDir>
#include <QFileInfo>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QString>
#include <QTemporaryDir>

#include <algorithm>
#include <cstdio>
#include <set>

using namespace FomodLogger;

namespace
{

int failures = 0;

void check(bool condition, const char* expression, int line)
{
  if (!condition) {
    ++failures;
    std::fprintf(stderr, "FAILED line %d: %s\n", line, expression);
  }
}
#define CHECK(expression) check(static_cast<bool>(expression), #expression, __LINE__)

QByteArray readFile(const QString& path)
{
  QFile file(path);
  return file.open(QIODevice::ReadOnly) ? file.readAll() : QByteArray();
}

bool writeFile(const QString& path, const QByteArray& bytes)
{
  QFile file(path);
  return file.open(QIODevice::WriteOnly) && file.write(bytes) == bytes.size();
}

const QString kInstance(64, QLatin1Char('a'));
const QString kOtherInstance(64, QLatin1Char('b'));

QByteArray event(const QString& id, const QString& instance, const QString& mod,
                 const QString& archive, const QString& completedAt,
                 const QString& optionName = QStringLiteral("Option"))
{
  QJsonObject option{{"id", "step:0/group:0/option:0"},
                     {"name", optionName},
                     {"selected", true},
                     {"enabled", true}};
  QJsonObject group{{"id", "step:0/group:0"},
                    {"name", "Group"},
                    {"type", "SelectAny"},
                    {"options", QJsonArray{option}}};
  QJsonObject step{{"id", "step:0"},
                   {"name", "Step"},
                   {"visible", true},
                   {"groups", QJsonArray{group}}};
  QJsonObject root{{"schema_version", 1},
                   {"event_id", id},
                   {"started_at", completedAt},
                   {"completed_at", completedAt},
                   {"instance", QJsonObject{{"id", instance}, {"label", "x"}}},
                   {"source", QJsonObject{{"archive", archive}, {"reinstallation", false}}},
                   {"mod", QJsonObject{{"name", mod}, {"version", "1"}, {"nexus_id", 1}}},
                   {"installer", QJsonObject{{"name", "n"}, {"version", "1"},
                                             {"upstream_commit", "unknown"}}},
                   {"steps", QJsonArray{step}}};
  return QJsonDocument(root).toJson(QJsonDocument::Compact);
}

// An imported-choices record for one mod, as import_choices.py writes it.
QByteArray importedRecord(const QString& instance, const QString& mod, int nexusId,
                          const QString& archive, const QString& importedAt,
                          const QString& optionName = QStringLiteral("Option"))
{
  QJsonObject option{{"name", optionName}, {"picked", true}, {"enabled", true}};
  QJsonObject group{{"name", "Group"}, {"type", "SelectAny"}, {"options", QJsonArray{option}}};
  QJsonObject step{{"name", "Step"}, {"skipped", false}, {"groups", QJsonArray{group}}};
  QJsonObject entry{{"name", mod},
                    {"nexus_id", nexusId},
                    {"version", "1"},
                    {"archive", archive},
                    {"installed_at", importedAt},
                    {"steps", QJsonArray{step}}};
  QJsonObject root{{"format", "fomod-companion-imported-choices"},
                   {"record_version", 1},
                   {"instance_id", instance},
                   {"imported_at", importedAt},
                   {"source_exported_at", importedAt},
                   {"entry", entry}};
  return QJsonDocument(root).toJson(QJsonDocument::Compact);
}

void testOccurrences()
{
  OccurrenceCounter counter;
  CHECK(counter.next("S", "G", "O") == OptionPath("S", "G", "O", 0));
  CHECK(counter.next("S", "G", "O") == OptionPath("S", "G", "O", 1));
  CHECK(counter.next("S", "G", "P") == OptionPath("S", "G", "P", 0));
  CHECK(counter.next("T", "G", "O") == OptionPath("T", "G", "O", 0));
  CHECK(counter.next("S", "G", "O") == OptionPath("S", "G", "O", 2));
  // Names are compared exactly: case and empty labels are distinct, not errors.
  CHECK(counter.next("S", "G", "o") == OptionPath("S", "G", "o", 0));
  CHECK(counter.next("", "", "") == OptionPath("", "", "", 0));
  CHECK(counter.next("", "", "") == OptionPath("", "", "", 1));
}

void testFixtureMatchesTheSharedExpectation(const QString& fixtures)
{
  const auto install =
      parsePreviousInstall(readFile(fixtures + "/duplicate_labels_event.json"));
  CHECK(install.has_value());
  const QJsonArray expected =
      QJsonDocument::fromJson(readFile(fixtures + "/duplicate_labels_paths.json"))
          .object()
          .value("options")
          .toArray();
  CHECK(expected.size() == 8);
  if (!install) {
    return;
  }
  CHECK(install->options.size() == static_cast<std::size_t>(expected.size()));
  CHECK(install->modName == "Duplicate Labels");
  CHECK(install->archive == "Duplicate Labels-1.0.zip");
  CHECK(install->completedAt.isValid());
  for (const QJsonValue& value : expected) {
    const QJsonArray row = value.toArray();
    const OptionPath path(row[0].toString(), row[1].toString(), row[2].toString(),
                          row[3].toInt());
    const PreviousOption* found = install->find(path);
    CHECK(found != nullptr);
    if (found != nullptr) {
      CHECK(found->selected == row[4].toBool());
      CHECK(found->enabled == row[5].toBool());
      CHECK(found->stepVisible == row[6].toBool());
    }
  }
  // A path nobody recorded finds nothing.
  CHECK(install->find(OptionPath("Main", "Textures", "2K", 1)) == nullptr);
  CHECK(install->find(OptionPath("main", "Textures", "2K", 0)) == nullptr);
}

void testParseRejectsWhatIsNotAnEvent()
{
  CHECK(!parsePreviousInstall(QByteArray()));
  CHECK(!parsePreviousInstall("not json"));
  CHECK(!parsePreviousInstall("[]"));
  CHECK(!parsePreviousInstall("{}"));
  CHECK(!parsePreviousInstall("{\"schema_version\": 2, \"steps\": []}"));
  const QByteArray good = event("11111111-1111-4111-8111-111111111111", kInstance,
                                "Mod", "a.zip", "2026-01-01T00:00:00.000Z");
  CHECK(parsePreviousInstall(good).has_value());
  QByteArray noTime = good;
  noTime.replace("completed_at", "completed_xx");
  CHECK(!parsePreviousInstall(noTime));
  // Older records had no fractional seconds or used an offset.
  CHECK(parsePreviousInstall(event("11111111-1111-4111-8111-111111111111", kInstance,
                                   "Mod", "a.zip", "2026-01-01T00:00:00+00:00")));
  CHECK(parsePreviousInstall(event("11111111-1111-4111-8111-111111111111", kInstance,
                                   "Mod", "a.zip", "2026-01-01T00:00:00Z")));
}

void testFindPicksNewestByNameThenArchive()
{
  QTemporaryDir data;
  CHECK(data.isValid());
  const QString root = data.path();

  PreviousQuery query;
  query.instanceId = kInstance;
  query.modNames   = {QStringLiteral("Foo")};
  query.archiveFileName = QStringLiteral("Foo-1.zip");

  // No folder at all.
  CHECK(!findPreviousInstall(root, query));

  const QString events = root + "/fomod_logger/events";
  CHECK(QDir().mkpath(events));
  const auto put = [&](const QString& name, const QByteArray& bytes) {
    CHECK(writeFile(events + "/" + name + ".json", bytes));
  };
  const auto id = [](int n) {
    return QStringLiteral("00000000-0000-4000-8000-%1").arg(n, 12, 10, QLatin1Char('0'));
  };

  put("a", event(id(1), kInstance, "Foo", "Foo-1.zip", "2026-01-01T00:00:00.000Z", "old"));
  put("b", event(id(2), kInstance, "foo", "other.zip", "2026-03-01T00:00:00.000Z", "newest"));
  put("c", event(id(3), kOtherInstance, "Foo", "Foo-1.zip", "2026-05-01T00:00:00.000Z"));
  put("d", "{ this is not json");
  put("e", "");
  put("f", event(id(6), kInstance, "Bar", "FOO-1.ZIP", "2026-07-01T00:00:00.000Z",
                 "archive"));
  // Bigger than the cap, otherwise valid and newest: must be skipped, not read.
  QByteArray huge = event(id(7), kInstance, "Foo", "Foo-1.zip", "2026-08-01T00:00:00.000Z");
  huge.append(QByteArray(static_cast<int>(kMaxEventFileBytes), ' '));
  put("g", huge);
  put("h", "{\"schema_version\": 1}");
  // A patch hub file named after its target mod: the target's record is newer.
  put("i", event(id(9), kInstance, "Target Mod", "target.zip", "2026-09-01T00:00:00.000Z"));
  put("j", event(id(10), kInstance, "Patch Hub", "hub.zip", "2026-02-01T00:00:00.000Z"));
  CHECK(writeFile(events + "/notes.txt", "ignored"));

  // The name match wins over a newer archive match; case does not matter.
  auto found = findPreviousInstall(root, query);
  CHECK(found.has_value());
  if (found) {
    CHECK(found->eventId == id(2));
    CHECK(found->find(OptionPath("Step", "Group", "newest", 0)) != nullptr);
  }

  // Any of the candidate names will do.
  query.modNames = {QStringLiteral("Something else"), QStringLiteral("  FOO ")};
  found          = findPreviousInstall(root, query);
  CHECK(found && found->eventId == id(2));

  // No name match: the archive file name decides, whatever folder it came from.
  query.modNames        = {QStringLiteral("Nothing")};
  query.archiveFileName = QStringLiteral("C:\\Downloads\\foo-1.zip");
  found                 = findPreviousInstall(root, query);
  CHECK(found && found->eventId == id(6));

  // Neither matches.
  query.archiveFileName = QStringLiteral("unrelated.zip");
  CHECK(!findPreviousInstall(root, query));
  query.archiveFileName.clear();
  CHECK(!findPreviousInstall(root, query));

  // Tiers, not a pool: the first tier with a record decides even when a later tier
  // holds a newer one.
  query.modNames        = {QStringLiteral("Patch Hub")};
  query.otherModNames   = {QStringLiteral("Target Mod")};
  query.archiveFileName = QStringLiteral("target.zip");
  found                 = findPreviousInstall(root, query);
  CHECK(found && found->eventId == id(10));
  // No tier-1 record: the other guesses are pooled among themselves, newest wins.
  query.modNames      = {QStringLiteral("Missing")};
  query.otherModNames = {QStringLiteral("Patch Hub"), QStringLiteral("Target Mod")};
  found               = findPreviousInstall(root, query);
  CHECK(found && found->eventId == id(9));
  // A name guess beats a newer archive match.
  query.otherModNames   = {QStringLiteral("Patch Hub")};
  query.archiveFileName = QStringLiteral("FOO-1.ZIP");
  found                 = findPreviousInstall(root, query);
  CHECK(found && found->eventId == id(10));
  // Only the archive left.
  query.otherModNames = {QStringLiteral("Nobody")};
  found               = findPreviousInstall(root, query);
  CHECK(found && found->eventId == id(6));
  query.modNames.clear();
  query.otherModNames.clear();
  query.archiveFileName.clear();
  CHECK(!findPreviousInstall(root, query));

  // A different instance only sees its own records.
  query.instanceId = kOtherInstance;
  query.modNames   = {QStringLiteral("Foo")};
  found            = findPreviousInstall(root, query);
  CHECK(found && found->eventId == id(3));
  query.instanceId = QString();
  CHECK(!findPreviousInstall(root, query));
}

void testImportedFixtureMatchesTheSharedExpectation(const QString& fixtures)
{
  // imported_record.json is what the Python import writes for the choices exported from
  // duplicate_labels_event.json, so it must give exactly the same option paths.
  const auto install = parseImportedInstall(readFile(fixtures + "/imported_record.json"));
  CHECK(install.has_value());
  const QJsonArray expected =
      QJsonDocument::fromJson(readFile(fixtures + "/duplicate_labels_paths.json"))
          .object()
          .value("options")
          .toArray();
  if (!install) {
    return;
  }
  CHECK(install->imported);
  CHECK(install->modName == "Duplicate Labels");
  CHECK(install->archive == "Duplicate Labels-1.0.zip");
  CHECK(install->nexusId == 1);
  CHECK(install->instanceId == kInstance);
  CHECK(install->completedAt.isValid());
  CHECK(install->options.size() == static_cast<std::size_t>(expected.size()));
  for (const QJsonValue& value : expected) {
    const QJsonArray row = value.toArray();
    const OptionPath path(row[0].toString(), row[1].toString(), row[2].toString(),
                          row[3].toInt());
    const PreviousOption* found = install->find(path);
    CHECK(found != nullptr);
    if (found != nullptr) {
      CHECK(found->selected == row[4].toBool());
      CHECK(found->enabled == row[5].toBool());
      CHECK(found->stepVisible == row[6].toBool());
    }
  }
}

void testParseImportedRejects()
{
  const QByteArray good = importedRecord(kInstance, "Mod", 5, "a.zip", "2026-01-01T00:00:00Z");
  CHECK(parseImportedInstall(good).has_value());
  CHECK(!parseImportedInstall(QByteArray()));
  CHECK(!parseImportedInstall("not json"));
  CHECK(!parseImportedInstall("[]"));
  CHECK(!parseImportedInstall("{}"));
  // An install event is not an imported record, and the other way round.
  CHECK(!parseImportedInstall(event("11111111-1111-4111-8111-111111111111", kInstance,
                                    "Mod", "a.zip", "2026-01-01T00:00:00.000Z")));
  CHECK(!parsePreviousInstall(good));
  const auto broken = [&](const char* from, const char* to) {
    QByteArray bytes = good;
    bytes.replace(from, to);
    return parseImportedInstall(bytes).has_value();
  };
  CHECK(!broken("fomod-companion-imported-choices", "something-else"));
  CHECK(!broken("\"record_version\":1", "\"record_version\":2"));
  CHECK(!broken("\"imported_at\"", "\"imported_xx\""));
  CHECK(!broken("\"instance_id\"", "\"instance_xx\""));
  CHECK(!broken("\"steps\"", "\"stepz\""));
  CHECK(!parseImportedInstall(importedRecord(kInstance, "  ", 5, "a.zip", "2026-01-01T00:00:00Z")));

  // An entry with no steps at all is valid and offers nothing.
  QJsonObject record = QJsonDocument::fromJson(good).object();
  QJsonObject entry  = record.value("entry").toObject();
  entry.insert("steps", QJsonArray());
  record.insert("entry", entry);
  const auto empty = parseImportedInstall(QJsonDocument(record).toJson());
  CHECK(empty.has_value() && empty->options.empty());
  // A missing picked/enabled flag reads as not picked / enabled; a skipped step is not shown.
  entry.insert("steps",
               QJsonArray{QJsonObject{
                   {"name", "S"},
                   {"skipped", true},
                   {"groups", QJsonArray{QJsonObject{
                                  {"name", "G"},
                                  {"options", QJsonArray{QJsonObject{{"name", "O"}}}}}}}}});
  record.insert("entry", entry);
  const auto sparse = parseImportedInstall(QJsonDocument(record).toJson());
  CHECK(sparse.has_value());
  if (sparse) {
    const PreviousOption* option = sparse->find(OptionPath("S", "G", "O", 0));
    CHECK(option != nullptr);
    if (option != nullptr) {
      CHECK(!option->selected && option->enabled && !option->stepVisible);
    }
  }
}

bool hasOption(const std::optional<PreviousInstall>& install, const char* name)
{
  return install &&
         install->find(OptionPath("Step", "Group", QString::fromLatin1(name), 0)) != nullptr;
}

void testFindImportedByNexusIdThenArchiveThenName()
{
  QTemporaryDir data;
  CHECK(data.isValid());
  const QString root = data.path();

  PreviousQuery query;
  query.instanceId      = kInstance;
  query.modNames        = {QStringLiteral("Foo")};
  query.archiveFileName = QStringLiteral("Foo-1.zip");
  query.nexusId         = 100;

  // No folder yet, and a folder of the user's own events is not imported choices.
  CHECK(!findImportedInstall(root, query));
  CHECK(QDir().mkpath(root + "/fomod_logger/events"));
  CHECK(writeFile(root + "/fomod_logger/events/own.json",
                  event("11111111-1111-4111-8111-111111111111", kInstance, "Foo", "Foo-1.zip",
                        "2026-01-01T00:00:00.000Z")));
  CHECK(!findImportedInstall(root, query));

  const QString imported = root + "/fomod_logger/imported";
  CHECK(QDir().mkpath(imported));
  const auto put = [&](const QString& name, const QByteArray& bytes) {
    CHECK(writeFile(imported + "/" + name + ".json", bytes));
  };
  // Option names tell the records apart in the results.
  put("a", importedRecord(kInstance, "Foo", 0, "Foo-1.zip", "2026-01-01T00:00:00Z", "byName"));
  put("b", importedRecord(kInstance, "Other", 0, "foo-1.ZIP", "2026-02-01T00:00:00Z", "byArchive"));
  put("c", importedRecord(kInstance, "Renamed", 100, "renamed.zip", "2026-01-15T00:00:00Z", "byNexus"));
  put("d", importedRecord(kOtherInstance, "Renamed", 100, "renamed.zip", "2026-09-01T00:00:00Z", "otherInstance"));
  put("e", "{ this is not json");
  put("f", "");
  // Bigger than the cap, otherwise valid and newest: skipped, not read.
  QByteArray huge = importedRecord(kInstance, "Renamed", 100, "renamed.zip", "2026-10-01T00:00:00Z", "huge");
  huge.append(QByteArray(static_cast<int>(kMaxEventFileBytes), ' '));
  put("g", huge);
  CHECK(writeFile(imported + "/notes.txt", "ignored"));

  // The record with the Nexus ID agrees with the query on neither archive nor name, while
  // others do: the ID is not believed on its own, so the archive match wins.
  auto found = findImportedInstall(root, query);
  CHECK(found && hasOption(found, "byArchive") && found->imported);
  CHECK(found && found->eventId == "b.json");
  // The records of another instance are never seen.
  CHECK(!hasOption(found, "otherInstance"));

  // The same ID with the right archive is believed, even though it is older than the
  // other archive match.
  put("n", importedRecord(kInstance, "Foo Old", 100, "Foo-1.zip", "2026-01-02T00:00:00Z", "agreedNexus"));
  found = findImportedInstall(root, query);
  CHECK(found && hasOption(found, "agreedNexus") && found->eventId == "n.json");
  CHECK(QFile::remove(imported + "/n.json"));

  // Nothing but the ID matches: the record that has it is the last resort.
  PreviousQuery idOnly;
  idOnly.instanceId      = kInstance;
  idOnly.nexusId         = 100;
  idOnly.archiveFileName = QStringLiteral("something-else.zip");
  idOnly.modNames        = {QStringLiteral("Somebody")};
  found                  = findImportedInstall(root, idOnly);
  CHECK(found && hasOption(found, "byNexus") && found->eventId == "c.json");

  // No Nexus ID known (or none matches): the archive file name, in any case and folder.
  query.nexusId = 0;
  found         = findImportedInstall(root, query);
  CHECK(found && hasOption(found, "byArchive"));
  query.nexusId         = 999;
  query.archiveFileName = QStringLiteral("C:\\Downloads\\FOO-1.zip");
  found                 = findImportedInstall(root, query);
  CHECK(found && hasOption(found, "byArchive"));

  // Then the name, the first tier of guesses before the second.
  query.archiveFileName = QStringLiteral("nothing.zip");
  found                 = findImportedInstall(root, query);
  CHECK(found && hasOption(found, "byName"));
  query.modNames      = {QStringLiteral("Nobody")};
  query.otherModNames = {QStringLiteral("  other ")};
  found               = findImportedInstall(root, query);
  CHECK(found && hasOption(found, "byArchive"));
  query.modNames.clear();
  query.otherModNames.clear();
  CHECK(!findImportedInstall(root, query));
  query.archiveFileName.clear();
  query.nexusId = 0;
  CHECK(!findImportedInstall(root, query));

  // The newest import of a mod wins within a tier.
  put("h", importedRecord(kInstance, "Foo", 0, "Foo-1.zip", "2026-03-01T00:00:00Z", "newerByName"));
  query.modNames = {QStringLiteral("foo")};
  found          = findImportedInstall(root, query);
  CHECK(found && hasOption(found, "newerByName"));

  // An unknown instance sees nothing.
  query.instanceId = QString();
  CHECK(!findImportedInstall(root, query));
}

void testSeveralModsOnOneNexusPage()
{
  QTemporaryDir data;
  CHECK(data.isValid());
  const QString imported = data.path() + "/fomod_logger/imported";
  CHECK(QDir().mkpath(imported));
  CHECK(writeFile(imported + "/main.json",
                  importedRecord(kInstance, "Big Mod", 7, "Big-Mod-main.zip",
                                 "2026-01-01T00:00:00Z", "main")));
  CHECK(writeFile(imported + "/patches.json",
                  importedRecord(kInstance, "Big Mod Patches", 7, "Big-Mod-patches.zip",
                                 "2026-02-01T00:00:00Z", "patches")));

  PreviousQuery query;
  query.instanceId = kInstance;
  query.nexusId    = 7;
  // The archive tells them apart.
  query.archiveFileName = QStringLiteral("big-mod-main.zip");
  auto found            = findImportedInstall(data.path(), query);
  CHECK(found && hasOption(found, "main") && !hasOption(found, "patches"));
  // Failing that, the name does.
  query.archiveFileName = QStringLiteral("renamed-download.zip");
  query.modNames        = {QStringLiteral("Big Mod Patches")};
  found                 = findImportedInstall(data.path(), query);
  CHECK(found && hasOption(found, "patches") && !hasOption(found, "main"));
  // Neither: the Nexus ID alone cannot say which, so it decides nothing.
  query.modNames.clear();
  CHECK(!findImportedInstall(data.path(), query));
  // A single record on the page needs no help.
  CHECK(QFile::remove(imported + "/patches.json"));
  found = findImportedInstall(data.path(), query);
  CHECK(found && hasOption(found, "main"));
}

void testNexusIdAloneIsTheLastResort()
{
  QTemporaryDir data;
  CHECK(data.isValid());
  const QString imported = data.path() + "/fomod_logger/imported";
  CHECK(QDir().mkpath(imported));
  const auto put = [&](const QString& name, const QByteArray& bytes) {
    CHECK(writeFile(imported + "/" + name + ".json", bytes));
  };
  put("x", importedRecord(kInstance, "X", 5, "x.zip", "2026-01-01T00:00:00Z", "x"));
  put("y", importedRecord(kInstance, "Y", 0, "y.zip", "2026-02-01T00:00:00Z", "y"));

  PreviousQuery query;
  query.instanceId = kInstance;
  query.nexusId    = 5;

  // Nothing at all to look the mod up by: no answer, whatever is stored.
  PreviousQuery blank;
  blank.instanceId = kInstance;
  CHECK(!findImportedInstall(data.path(), blank));

  // The ID alone, with nothing else matching: the one record that has it.
  query.archiveFileName = QStringLiteral("other.zip");
  query.modNames        = {QStringLiteral("Nobody")};
  auto found            = findImportedInstall(data.path(), query);
  CHECK(found && hasOption(found, "x"));

  // A different record that matches by archive beats the lone ID.
  query.archiveFileName = QStringLiteral("y.zip");
  found                 = findImportedInstall(data.path(), query);
  CHECK(found && hasOption(found, "y"));
  // ... and so does one that matches by name.
  query.archiveFileName.clear();
  query.modNames = {QStringLiteral("y")};
  found          = findImportedInstall(data.path(), query);
  CHECK(found && hasOption(found, "y"));
  query.otherModNames = {QStringLiteral("Nobody")};
  query.modNames      = {QStringLiteral("Nobody")};
  found               = findImportedInstall(data.path(), query);
  CHECK(found && hasOption(found, "x"));

  // The ID plus the archive (or the name) agreeing is believed, even over a newer record
  // that matches by archive alone.
  put("z", importedRecord(kInstance, "Z", 5, "y.zip", "2025-12-01T00:00:00Z", "z"));
  query.archiveFileName = QStringLiteral("y.zip");
  found                 = findImportedInstall(data.path(), query);
  CHECK(found && hasOption(found, "z"));
  query.archiveFileName.clear();
  query.modNames = {QStringLiteral("X")};
  found          = findImportedInstall(data.path(), query);
  CHECK(found && hasOption(found, "x"));

  // Two records share the ID and nothing else says which is meant: the ID decides nothing.
  query.modNames.clear();
  query.otherModNames.clear();
  query.archiveFileName = QStringLiteral("unrelated.zip");
  CHECK(!findImportedInstall(data.path(), query));
}

FileRecord fileRecord(const QString& source, const QString& destination, bool folder,
                      int priority, int order)
{
  FileRecord record;
  record.source      = source;
  record.destination = destination;
  record.resolved =
      resolveFileDestination(source, destination, folder, &record.escapes);
  record.isFolder = folder;
  record.priority = priority;
  record.order    = order;
  return record;
}

QString resolved(const QString& source, const QString& destination, bool folder,
                 bool* escapes = nullptr)
{
  return resolveFileDestination(source, destination, folder, escapes);
}

void testResolveFileDestination()
{
  // A file keeps the destination it was given, with slashes normalised and case kept.
  CHECK(resolved("a\\b.dds", "Textures\\Foo\\Bar.dds", false) == "Textures/Foo/Bar.dds");
  // No destination (empty, or ending in a slash): the file keeps its own name.
  CHECK(resolved("2K\\bar.dds", "", false) == "bar.dds");
  CHECK(resolved("2K/bar.dds", "textures/foo/", false) == "textures/foo/bar.dds");
  CHECK(resolved("bar.dds", "textures\\foo\\", false) == "textures/foo/bar.dds");
  // A folder lands where it says; a trailing slash is dropped; "" is the mod folder.
  CHECK(resolved("2K\\Textures", "Textures\\", true) == "Textures");
  CHECK(resolved("Data", "", true).isEmpty());
  CHECK(resolved("Data", ".", true).isEmpty());
  CHECK(resolved("Data", "/", true).isEmpty());
  // Doubled slashes and "." parts are tidied; a leading slash is not kept.
  CHECK(resolved("x", "/textures//./foo/x.dds", false) == "textures/foo/x.dds");
  // A FILE whose destination names the mod folder keeps its own name: it never resolves
  // to "" (which would read as the mod folder itself).
  CHECK(resolved("core\\x.dds", ".", false) == "x.dds");
  CHECK(resolved("core\\x.dds", "/", false) == "x.dds");
  CHECK(resolved("core\\x.dds", "\\", false) == "x.dds");
  CHECK(resolved("core\\x.dds", "Data", false) == "x.dds");
  CHECK(resolved("core\\x.dds", "Data/", false) == "x.dds");
  // A leading Data/ is dropped (the installer does the same), whatever the case.
  CHECK(resolved("a", "Data\\Textures\\a.dds", false) == "Textures/a.dds");
  CHECK(resolved("a", "DATA/textures/a.dds", false) == "textures/a.dds");
  CHECK(resolved("Fix", "data/textures", true) == "textures");
  CHECK(resolved("Fix", "Data", true).isEmpty());
  CHECK(resolved("a", "Database/a.dds", false) == "Database/a.dds");  // not the Data folder
  CHECK(resolved("a", "textures/data/a.dds", false) == "textures/data/a.dds");
  // Tidying inside the folder is fine; leaving it is not.
  CHECK(resolved("a", "textures/../meshes/a.nif", false) == "meshes/a.nif");

  // Hostile destinations are never recorded as if they were relative paths.
  for (const bool folder : {false, true}) {
    for (const QString& hostile :
         {QStringLiteral("../x.dds"), QStringLiteral("..\\..\\x"), QStringLiteral(".."),
          QStringLiteral("textures/../../x.dds"), QStringLiteral("C:/Windows/x.dds"),
          QStringLiteral("c:\\Windows\\x.dds"), QStringLiteral("C:x.dds"),
          QStringLiteral("\\\\server\\share\\x.dds"), QStringLiteral("//server/share/x"),
          // A ".." right after a leading slash is still a way out of the mod folder.
          QStringLiteral("/../x.dds"), QStringLiteral("\\..\\x"), QStringLiteral("/.."),
          QStringLiteral("/a/../../x.dds")}) {
      bool escapes = false;
      CHECK(resolved("a", hostile, folder, &escapes).isEmpty());
      CHECK(escapes);
    }
  }
  bool escapes = true;
  CHECK(resolved("a", "textures/a.dds", false, &escapes) == "textures/a.dds" && !escapes);
  escapes = true;
  CHECK(resolved("Data", "", true, &escapes).isEmpty() && !escapes);  // the mod folder
}

PatchEvidence manyFiles(const QString& id, int count, bool installed, int priority = 0)
{
  PatchEvidence entry;
  entry.id        = id;
  entry.installed = installed;
  entry.hasFiles  = true;
  for (int i = 0; i < count; ++i) {
    entry.files.append(fileRecord("s", "d" + QString::number(i) + ".dds", false, priority, i));
  }
  return entry;
}

void testCapFileRecords()
{
  const qint64 plenty = 1LL << 40;  // the count caps on their own
  QList<PatchEvidence> evidence;
  evidence.append(manyFiles("small", 3, true));
  evidence.append(manyFiles("none", 0, true));
  PatchEvidence unrecorded;
  unrecorded.id = "unrecorded";
  evidence.append(unrecorded);
  evidence.append(manyFiles("big", int(kMaxFilesPerOption) + 5, true));
  capFileRecords(evidence, plenty);
  CHECK(evidence[0].files.size() == 3 && !evidence[0].filesTruncated);
  CHECK(evidence[1].files.isEmpty() && !evidence[1].filesTruncated);
  CHECK(!evidence[2].hasFiles && !evidence[2].filesTruncated);
  CHECK(evidence[3].files.size() == kMaxFilesPerOption && evidence[3].filesTruncated);
  // Entries that were cut are the ones that lose: with equal priority the earliest in the
  // XML; the kept ones stay in their original order.
  CHECK(evidence[3].files.first().order == 5 &&
        evidence[3].files.last().order == int(kMaxFilesPerOption) + 4);

  // The event as a whole is capped too.
  evidence.clear();
  for (int i = 0; i < 12; ++i) {
    evidence.append(manyFiles("many" + QString::number(i), int(kMaxFilesPerOption), true));
  }
  capFileRecords(evidence, plenty);
  qsizetype total = 0;
  for (const PatchEvidence& entry : evidence) {
    total += entry.files.size();
  }
  CHECK(total == kMaxFilesPerEvent);
  CHECK(evidence[9].files.size() == kMaxFilesPerOption && !evidence[9].filesTruncated);
  CHECK(evidence[10].files.isEmpty() && evidence[10].filesTruncated);
  CHECK(evidence[11].files.isEmpty() && evidence[11].filesTruncated);
}

void testCapKeepsTheEntriesThatWin()
{
  // An option's list arrives sorted by (priority, order), lowest first, so the entries that
  // win are at the end. Cutting must drop the losers, not the winners.
  PatchEvidence item;
  item.id       = "option";
  item.hasFiles = true;
  for (int i = 0; i < int(kMaxFilesPerOption) + 10; ++i) {
    item.files.append(fileRecord("s", "d" + QString::number(i) + ".dds", false,
                                 i / 100, 1000 + i));  // priority rises with position
  }
  item.files.append(fileRecord("s", "winner.dds", false, 99, 5));  // highest priority, early in the XML
  QList<PatchEvidence> evidence{item};
  capFileRecords(evidence, 1LL << 40);
  const PatchEvidence& cut = evidence[0];
  CHECK(cut.filesTruncated && cut.files.size() == kMaxFilesPerOption);
  bool hasWinner = false;
  int lowest     = 1 << 30;
  for (const FileRecord& record : cut.files) {
    hasWinner = hasWinner || record.destination == QLatin1String("winner.dds");
    lowest    = (std::min)(lowest, record.priority);
  }
  CHECK(hasWinner);
  CHECK(lowest >= 0);
  CHECK(cut.files.first().destination == QLatin1String("d11.dds"));  // original order kept
  CHECK(cut.files.last().destination == QLatin1String("winner.dds"));

  // The same through a byte budget that only fits three entries: the three that win stay.
  PatchEvidence small;
  small.id       = "option";
  small.hasFiles = true;
  small.files.append(fileRecord("s", "a.dds", false, 0, 1));
  small.files.append(fileRecord("s", "b.dds", false, 0, 2));
  small.files.append(fileRecord("s", "c.dds", false, 7, 3));
  small.files.append(fileRecord("s", "d.dds", false, 0, 4));
  small.files.append(fileRecord("s", "e.dds", false, 7, 5));
  QList<PatchEvidence> one{small};
  capFileRecords(one, 3 * (kFileEntryOverheadBytes + 20));
  CHECK(one[0].filesTruncated && one[0].files.size() == 3);
  CHECK(one[0].files[0].destination == QLatin1String("c.dds") &&
        one[0].files[1].destination == QLatin1String("d.dds") &&
        one[0].files[2].destination == QLatin1String("e.dds"));
}

void testCapSpendsTheBudgetOnWhatIsInstalledFirst()
{
  // Required files first, then picked options and applied automatic installs, then the
  // options that were skipped: later in the event must not mean lost.
  const qint64 room = 10 * (kFileEntryOverheadBytes + 20);
  QList<PatchEvidence> evidence;
  evidence.append(manyFiles("skipped", 8, false));
  evidence.append(manyFiles("picked", 6, true));
  evidence.append(manyFiles("conditional:0", 6, true));
  evidence.append(manyFiles(kRequiredFilesId, 3, true));
  capFileRecords(evidence, room);
  CHECK(evidence[3].files.size() == 3 && !evidence[3].filesTruncated);          // required: all
  CHECK(evidence[1].files.size() == 6 && !evidence[1].filesTruncated);          // picked: all
  CHECK(evidence[2].files.size() == 1 && evidence[2].filesTruncated);           // applied: what is left
  CHECK(evidence[0].files.isEmpty() && evidence[0].filesTruncated);             // skipped: nothing
  CHECK(evidence[1].files.size() + evidence[2].files.size() + evidence[3].files.size() == 10);
}

QJsonArray sampleSteps()
{
  const auto option = [](const QString& id, const QString& name, bool selected) {
    return QJsonObject{{"id", id}, {"name", name}, {"selected", selected}, {"enabled", true}};
  };
  QJsonObject group{{"id", "step:0/group:0"},
                    {"name", "Texture resolution"},
                    {"type", "SelectExactlyOne"},
                    {"options", QJsonArray{option("step:0/group:0/option:0", "1K", false),
                                           option("step:0/group:0/option:1", "2K", true),
                                           option("step:0/group:0/option:2", "None", false)}}};
  QJsonObject step{{"id", "step:0"},
                   {"name", "Textures"},
                   {"visible", true},
                   {"groups", QJsonArray{group}}};
  return QJsonArray{step};
}

QList<PatchEvidence> sampleEvidence()
{
  PatchEvidence oneK;
  oneK.id       = "step:0/group:0/option:0";
  oneK.hasFiles = true;
  oneK.files.append(fileRecord("1K\\Textures", "Textures", true, 0, 1));
  PatchEvidence twoK;
  twoK.id        = "step:0/group:0/option:1";
  twoK.installed = true;
  twoK.plugins   = {"Majestic Mountains.esp"};
  twoK.hasFiles  = true;
  twoK.files.append(fileRecord("2K\\Textures", "Textures", true, 5, 2));
  twoK.files.append(fileRecord("2K\\Majestic Mountains.esp", "", false, 0, 3));
  PatchEvidence none;  // an option that installs nothing
  none.id       = "step:0/group:0/option:2";
  none.hasFiles = true;
  PatchEvidence automatic;
  automatic.id        = "conditional:0";
  automatic.installed = true;
  automatic.hasFiles  = true;
  automatic.files.append(
      fileRecord("Patches\\SMIM.esp", "Data\\SMIM Patch.esp", false, 2, 4));
  automatic.filesTruncated = true;
  PatchEvidence unrecorded;  // evidence from before files were recorded
  unrecorded.id = "conditional:1";
  PatchEvidence required;
  required.id        = kRequiredFilesId;
  required.installed = true;
  required.hasFiles  = true;
  required.files.append(fileRecord("Core\\core.dds", "Textures\\core.dds", false, 0, 0));
  PatchEvidence outside;  // a hostile entry: recorded, but not as a relative path
  outside.id       = "conditional:2";
  outside.hasFiles = true;
  outside.files.append(fileRecord("x.dds", "..\\..\\x.dds", false, 0, 9));
  return {oneK, twoK, none, automatic, unrecorded, required, outside};
}

QJsonObject findOption(const QJsonArray& steps, const QString& id)
{
  for (const QJsonValue& step : steps) {
    for (const QJsonValue& group : step.toObject().value("groups").toArray()) {
      for (const QJsonValue& option : group.toObject().value("options").toArray()) {
        if (option.toObject().value("id").toString() == id) {
          return option.toObject();
        }
      }
    }
  }
  return {};
}

void testEvidenceCarriesTheFilesOfEachOption()
{
  const QJsonArray steps = attachPatchEvidence(sampleSteps(), sampleEvidence());
  const QJsonObject oneK = findOption(steps, "step:0/group:0/option:0");
  CHECK(oneK.contains("files") && oneK.value("files").toArray().size() == 1);
  CHECK(!oneK.contains("files_truncated"));
  const QJsonObject entry = oneK.value("files").toArray().first().toObject();
  CHECK(entry.value("source").toString() == "1K\\Textures");  // as written
  CHECK(entry.value("destination").toString() == "Textures");
  CHECK(entry.value("resolved").toString() == "Textures");
  CHECK(entry.value("kind").toString() == "folder");
  CHECK(entry.value("order").toInt() == 1);
  CHECK(!entry.contains("priority"));  // 0 is the default and is left out

  const QJsonArray twoK =
      findOption(steps, "step:0/group:0/option:1").value("files").toArray();
  CHECK(twoK.size() == 2);
  CHECK(twoK[0].toObject().value("priority").toInt() == 5);
  CHECK(twoK[1].toObject().value("kind").toString() == "file");
  CHECK(twoK[1].toObject().value("resolved").toString() == "Majestic Mountains.esp");

  // An option that installs nothing says so with an empty list.
  const QJsonObject none = findOption(steps, "step:0/group:0/option:2");
  CHECK(none.contains("files") && none.value("files").toArray().isEmpty());

  // Evidence with no recorded files adds no `files` key at all.
  PatchEvidence old;
  old.id      = "step:0/group:0/option:0";
  old.plugins = {"x.esp"};
  const QJsonObject plain =
      findOption(attachPatchEvidence(sampleSteps(), {old}), "step:0/group:0/option:0");
  CHECK(plain.contains("plugins") && !plain.contains("files") &&
        !plain.contains("files_truncated"));
}

void testAutomaticInstallsCarryFilesAndTheTruncatedFlag()
{
  const QJsonArray automatic = automaticInstalls(sampleEvidence());
  CHECK(automatic.size() == 3);  // the required files are not an automatic install
  const QJsonObject first = automatic[0].toObject();
  CHECK(first.value("files").toArray().size() == 1);
  CHECK(first.value("files_truncated").toBool());
  CHECK(first.value("files").toArray()[0].toObject().value("resolved").toString() ==
        "SMIM Patch.esp");  // the Data/ prefix is dropped
  const QJsonObject second = automatic[1].toObject();
  CHECK(!second.contains("files") && !second.contains("files_truncated"));
  // A destination outside the mod folder is flagged and not recorded as a relative path.
  const QJsonObject hostile = automatic[2].toObject().value("files").toArray()[0].toObject();
  CHECK(hostile.value("escapes").toBool() && hostile.value("resolved").toString().isEmpty());
  CHECK(hostile.value("destination").toString() == "..\\..\\x.dds");
  // Required files come out as their own object, with the usual list.
  const QJsonObject required = requiredFiles(sampleEvidence());
  CHECK(required.value("files").toArray().size() == 1);
  CHECK(required.value("files").toArray()[0].toObject().value("resolved").toString() ==
        "Textures/core.dds");
  CHECK(requiredFiles({}).isEmpty());
  PatchEvidence none;
  none.id = kRequiredFilesId;  // required files that were not recorded
  CHECK(requiredFiles({none}).isEmpty());
}

void testTheWriterKeepsTheFilesAndSampleEventIsWritten(const QString& emitFolder)
{
  QTemporaryDir data;
  CHECK(data.isValid());
  QJsonObject root{
      {"schema_version", 1},
      {"event_id", "6f1f2f0e-1111-4aaa-8bbb-0123456789ab"},
      {"started_at", "2026-10-21T10:00:00Z"},
      {"completed_at", "2026-10-21T10:01:00Z"},
      {"instance", QJsonObject{{"id", kInstance}, {"label", "x"}}},
      {"source", QJsonObject{{"archive", "mm.7z"}, {"reinstallation", false}}},
      {"mod", QJsonObject{{"name", "Majestic Mountains"}, {"version", "1"}, {"nexus_id", 1}}},
      {"installer", QJsonObject{{"name", "n"}, {"version", "1"}, {"upstream_commit", "unknown"}}},
      {"steps", attachPatchEvidence(sampleSteps(), sampleEvidence())},
      {"automatic_installs", automaticInstalls(sampleEvidence())},
      {"required_files", requiredFiles(sampleEvidence())}};
  const EventWriteResult result = writeEvent(data.path(), root);
  CHECK(result.success);
  const QJsonObject back = QJsonDocument::fromJson(readFile(result.path)).object();
  CHECK(back == root);
  CHECK(findOption(back.value("steps").toArray(), "step:0/group:0/option:1")
            .value("files")
            .toArray()
            .size() == 2);
  CHECK(back.value("automatic_installs").toArray()[0].toObject().contains("files"));
  CHECK(back.value("required_files").toObject().value("files").toArray().size() == 1);
  if (!emitFolder.isEmpty()) {
    CHECK(QDir().mkpath(emitFolder));
    CHECK(writeEvent(emitFolder, root).success);
  }
}

// The biggest event the recorder can write: files everywhere, long paths with backslashes
// (which JSON doubles). It must stay readable by the previous-choices reader.
QJsonObject eventWith(const QJsonArray& steps, const QJsonObject& required)
{
  QJsonObject root{
      {"schema_version", 1},
      {"event_id", "6f1f2f0e-2222-4aaa-8bbb-0123456789ab"},
      {"started_at", "2026-10-21T10:00:00Z"},
      {"completed_at", "2026-10-21T10:01:00Z"},
      {"instance", QJsonObject{{"id", kInstance}, {"label", "x"}}},
      {"source", QJsonObject{{"archive", "big.7z"}, {"reinstallation", false}}},
      {"mod", QJsonObject{{"name", "Big Pack"}, {"version", "1"}, {"nexus_id", 1}}},
      {"installer", QJsonObject{{"name", "n"}, {"version", "1"}, {"upstream_commit", "unknown"}}},
      {"steps", steps}};
  if (!required.isEmpty()) {
    root.insert("required_files", required);
  }
  return root;
}

QList<PatchEvidence> worstCase(QJsonArray* steps)
{
  const auto longText = [](const QString& stem, int i) {
    return stem + QString::number(i) + QStringLiteral("\\") + QString(150, QLatin1Char('d')) +
           QStringLiteral("\\") + QString(100, QLatin1Char('f')) + QStringLiteral(".dds");
  };
  QList<PatchEvidence> evidence;
  QJsonArray options;
  for (int o = 0; o < 20; ++o) {  // 20 options x 1000 entries = the 20,000 count cap
    PatchEvidence item;
    item.id        = QStringLiteral("step:0/group:0/option:%1").arg(o);
    item.installed = o == 0;
    item.hasFiles  = true;
    for (int i = 0; i < 1000; ++i) {
      FileRecord record;
      record.source      = longText(QStringLiteral("Source"), i);
      record.destination = longText(QStringLiteral("Textures\\Dest"), i);
      record.resolved    = resolveFileDestination(record.source, record.destination, false);
      record.priority    = i % 3;
      record.order       = o * 1000 + i;
      item.files.append(record);
    }
    evidence.append(item);
    options.append(QJsonObject{{"id", item.id},
                               {"name", QStringLiteral("Option %1").arg(o)},
                               {"selected", o == 0},
                               {"enabled", true}});
  }
  QJsonObject group{{"id", "step:0/group:0"}, {"name", "G"}, {"type", "SelectAny"},
                    {"options", options}};
  *steps = QJsonArray{QJsonObject{{"id", "step:0"}, {"name", "S"}, {"visible", true},
                                  {"groups", QJsonArray{group}}}};
  return evidence;
}

void testTheBiggestEventStaysReadable()
{
  QJsonArray steps;
  QList<PatchEvidence> evidence = worstCase(&steps);
  QList<PatchEvidence> uncapped = evidence;
  capFileRecords(uncapped, 1LL << 40);  // the count caps only: what was written before
  const qint64 before =
      QJsonDocument(eventWith(attachPatchEvidence(steps, uncapped), {})).toJson(QJsonDocument::Indented).size();

  capFileRecords(evidence);
  qsizetype entries = 0;
  for (const PatchEvidence& item : evidence) {
    entries += item.files.size();
  }
  CHECK(entries > 1000 && entries < kMaxFilesPerEvent);
  QTemporaryDir data;
  CHECK(data.isValid());
  const EventWriteResult result =
      writeEvent(data.path(), eventWith(attachPatchEvidence(steps, evidence), {}));
  CHECK(result.success);
  const qint64 after = QFileInfo(result.path).size();
  std::printf("worst-case event: %lld bytes before the byte budget, %lld after (%lld entries)\n",
              static_cast<long long>(before), static_cast<long long>(after),
              static_cast<long long>(entries));
  CHECK(after < kMaxEventFileBytes);
  CHECK(after < 5LL * 1024 * 1024 / 2);  // about 2.5 MiB: room beside the file entries
  CHECK(before > kMaxEventFileBytes);    // the budget is what keeps it readable

  // The reader still takes it: the picked option's files survived whole.
  CHECK(evidence[0].files.size() == 1000 && !evidence[0].filesTruncated);
  CHECK(evidence[19].filesTruncated);
}

}  // namespace

int main(int argc, char** argv)
{
  const QString fixtures = argc > 1 ? QString::fromLocal8Bit(argv[1])
                                    : QStringLiteral(FOMOD_LOGGER_TEST_FIXTURES);
  const QString sampleFolder = argc > 2 ? QString::fromLocal8Bit(argv[2]) : QString();
  testOccurrences();
  testFixtureMatchesTheSharedExpectation(fixtures);
  testParseRejectsWhatIsNotAnEvent();
  testFindPicksNewestByNameThenArchive();
  testImportedFixtureMatchesTheSharedExpectation(fixtures);
  testParseImportedRejects();
  testFindImportedByNexusIdThenArchiveThenName();
  testSeveralModsOnOneNexusPage();
  testNexusIdAloneIsTheLastResort();
  testResolveFileDestination();
  testCapFileRecords();
  testCapKeepsTheEntriesThatWin();
  testCapSpendsTheBudgetOnWhatIsInstalledFirst();
  testTheBiggestEventStaysReadable();
  testEvidenceCarriesTheFilesOfEachOption();
  testAutomaticInstallsCarryFilesAndTheTruncatedFlag();
  testTheWriterKeepsTheFilesAndSampleEventIsWritten(sampleFolder);
  if (failures != 0) {
    std::fprintf(stderr, "%d check(s) failed\n", failures);
    return 1;
  }
  std::puts("previous_choices_test: all checks passed");
  return 0;
}
