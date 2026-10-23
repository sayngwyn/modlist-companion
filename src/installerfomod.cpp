#include "installerfomod.h"

#include "fomodinstallerdialog.h"
#include "fomodloggereventwriter.h"
#include "fomodpreviouschoices.h"
#include "fomodpatchevidence.h"
#include "fomodloggerversion.h"
#include "imodinterface.h"
#include "imodlist.h"

#include <iinstallationmanager.h>
#include <log.h>
#include <report.h>
#include <utility.h>

#include <algorithm>

#include <QDateTime>
#include <QHash>
#include <QFileInfo>
#include <QImageReader>
#include <QJsonArray>
#include <QJsonObject>
#include <QMutexLocker>
#include <QStringList>
#include <QUuid>
#include <QtPlugin>

using namespace MOBase;

InstallerFomod::InstallerFomod() : m_MOInfo(nullptr) {}

bool InstallerFomod::init(IOrganizer* moInfo)
{
  m_MOInfo = moInfo;
  return true;
}

QString InstallerFomod::name() const
{
  return "FOMOD Logger Native Installer";
}

QString InstallerFomod::author() const
{
  return "sayngwyn; based on work by Tannin & thosrtanner";
}

QString InstallerFomod::description() const
{
  return tr("FOMOD installer with atomic, structured choice logging.");
}

VersionInfo InstallerFomod::version() const
{
  return VersionInfo(QStringLiteral(FOMOD_LOGGER_MO2_VERSION),
                     VersionInfo::SCHEME_REGULAR);
}

QString InstallerFomod::localizedName() const
{
  return tr("Modlist Companion FOMOD Installer");
}

bool InstallerFomod::allowAnyFile() const
{
  return m_MOInfo->pluginSetting(name(), "use_any_file").toBool();
}

bool InstallerFomod::checkDisabledMods() const
{
  return m_MOInfo->pluginSetting(name(), "see_disabled_mods").toBool();
}

bool InstallerFomod::restorePreviousChoices() const
{
  return m_MOInfo->pluginSetting(name(), "restore_previous_choices").toBool();
}

QList<PluginSetting> InstallerFomod::settings() const
{
  QList<PluginSetting> result;
  result.push_back(PluginSetting(
      "prefer",
      "handle FOMOD archives before MO2's own FOMOD installers (needed to log choices)",
      QVariant(true)));
  result.push_back(PluginSetting("use_any_file",
                                 "allow dependencies on any file, not just esp/esm",
                                 QVariant(false)));
  result.push_back(PluginSetting("see_disabled_mods",
                                 "treat disabled mods as inactive rather than missing",
                                 QVariant(false)));
  result.push_back(PluginSetting(
      "restore_previous_choices",
      "when a mod has been installed before, select the same options again "
      "automatically (the Select previous button is always available)",
      QVariant(true)));
  return result;
}

unsigned int InstallerFomod::priority() const
{
  // MO2's XML and C# FOMOD installers use 110 when preferred and 90 otherwise. Logger
  // must run ahead of both to see the choices; with "prefer" off it steps behind
  // both so they handle FOMODs and Logger only takes what they decline.
  return m_MOInfo->pluginSetting(name(), "prefer").toBool() ? 120 : 80;
}

bool InstallerFomod::isManualInstaller() const
{
  return false;
}

void InstallerFomod::onInstallationStart(QString const& archive, bool reinstallation,
                                         IModInterface* currentMod)
{
  m_InstallerUsed  = false;
  m_Archive        = archive;
  m_Reinstallation = reinstallation;
  m_ReinstalledModName = currentMod != nullptr ? currentMod->name() : QString();
  m_StartedAtUtc   = QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
  m_PendingEvent   = QJsonObject();
}

void InstallerFomod::onInstallationEnd(EInstallResult result, IModInterface* newMod)
{
  if (result == EInstallResult::RESULT_SUCCESS && m_InstallerUsed &&
      newMod != nullptr) {
    if (newMod->url().isEmpty()) {
      newMod->setUrl(m_Url);
    }

    if (!m_PendingEvent.isEmpty()) {
      QJsonObject mod = m_PendingEvent.value("mod").toObject();
      mod.insert("name", newMod->name());
      mod.insert("nexus_id", newMod->nexusId());
      mod.insert("version", newMod->version().canonicalString());
      m_PendingEvent.insert("mod", mod);
      m_PendingEvent.insert(
          "completed_at", QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs));

      const auto writeResult =
          FomodLogger::writeEvent(m_MOInfo->pluginDataPath(), m_PendingEvent);
      if (writeResult.success) {
        log::info("Modlist Companion wrote installation event to {}", writeResult.path);
      } else {
        const QString error =
            tr("The choices for \"%1\" were not saved: %2")
                .arg(newMod->name(), writeResult.error);
        {
          // activeProblems() is also called from MO2's diagnosis thread.
          QMutexLocker lock(&m_WriteErrorsMutex);
          m_WriteErrors.append(error);
        }
        log::error("Modlist Companion: {}", error);
        invalidate();
      }
    }
  }

  m_PendingEvent = QJsonObject();
}

std::shared_ptr<const IFileTree>
InstallerFomod::findFomodDirectory(std::shared_ptr<const IFileTree> tree) const
{
  auto entry = tree->find("fomod", FileTreeEntry::DIRECTORY);

  if (entry != nullptr) {
    return entry->astree();
  }

  if (tree->size() == 1 && tree->at(0)->isDir()) {
    return findFomodDirectory(tree->at(0)->astree());
  }
  return nullptr;
}

bool InstallerFomod::isArchiveSupported(std::shared_ptr<const IFileTree> tree) const
{
  tree = findFomodDirectory(tree);
  if (tree != nullptr) {
    return tree->exists("ModuleConfig.xml", FileTreeEntry::FILE);
  }
  return false;
}

void InstallerFomod::appendImageFiles(
    std::vector<std::shared_ptr<const FileTreeEntry>>& entries,
    std::shared_ptr<const IFileTree> tree) const
{
  static std::set<QString, FileNameComparator> imageSuffixes{"png", "jpg", "jpeg",
                                                             "gif", "bmp"};
  for (auto entry : *tree) {
    if (entry->isDir()) {
      appendImageFiles(entries, entry->astree());
    } else if (imageSuffixes.count(entry->suffix()) > 0) {
      entries.push_back(entry);
    }
  }
}

std::vector<std::shared_ptr<const FileTreeEntry>>
InstallerFomod::buildFomodTree(std::shared_ptr<const IFileTree> tree) const
{
  std::vector<std::shared_ptr<const FileTreeEntry>> entries;

  auto fomodTree = findFomodDirectory(tree);

  for (auto entry : *fomodTree) {
    if (entry->isFile() &&
        (entry->compare("info.xml") == 0 || entry->compare("ModuleConfig.xml") == 0)) {
      entries.push_back(entry);
    }
  }

  appendImageFiles(entries, tree);

  return entries;
}

void InstallerFomod::offerPreviousChoices(
    FomodInstallerDialog& dialog, const GuessedValue<QString>& modName, int nexusId) const
{
  try {
    FomodLogger::PreviousQuery query;
    query.instanceId = FomodLogger::instanceId(m_MOInfo->basePath());
    // The install is recorded under whatever name the user ends up with. Search the
    // likeliest names first and stop at the first tier that has a record: MO2 can
    // also guess the name of a different mod (a patch hub file named after its
    // target, say), and pooling every guess would let that mod's newer record win.
    if (!m_ReinstalledModName.isEmpty()) {
      query.modNames.append(m_ReinstalledModName);
    }
    query.modNames.append(QString(modName));
    query.modNames.append(dialog.infoName());
    for (const QString& variant : modName.variants()) {
      query.otherModNames.append(variant);
    }
    query.archiveFileName = QFileInfo(m_Archive).fileName();
    query.nexusId         = nexusId;

    // The user's own earlier install of this mod is used when it lines up with something
    // in this installer. When there is none, or it matches nothing here (a different
    // version of the mod, say), imported choices are offered instead; those are only ever
    // offered, never restored automatically.
    const auto previous =
        FomodLogger::findPreviousInstall(m_MOInfo->pluginDataPath(), query);
    if (previous && dialog.setPreviousChoices(*previous, restorePreviousChoices())) {
      return;
    }
    const auto imported =
        FomodLogger::findImportedInstall(m_MOInfo->pluginDataPath(), query);
    if (imported) {
      dialog.setPreviousChoices(*imported, false);
    }
  } catch (const std::exception& e) {
    // A convenience only; the installation goes on without it.
    log::warn("Modlist Companion could not look up previous choices: {}", e.what());
  }
}

IPluginList::PluginStates InstallerFomod::fileState(const QString& fileName) const
{
  QString ext = QFileInfo(fileName).suffix().toLower();
  if ((ext == "esp") || (ext == "esm") || (ext == "esl")) {
    IPluginList::PluginStates state = m_MOInfo->pluginList()->state(fileName);
    if (state != IPluginList::STATE_MISSING) {
      return state;
    }
  } else if (allowAnyFile()) {
    QFileInfo info(fileName);
    QString name = info.fileName();
    QStringList files =
        m_MOInfo->findFiles(info.dir().path(), [&, name](const QString& f) -> bool {
          return name.compare(QFileInfo(f).fileName(),
                              FileNameComparator::CaseSensitivity) == 0;
        });
    // A note: The list of files produced is somewhat odd as it's the full path
    // to the originating mod (or mods). However, all we care about is if it's
    // there or not.
    if (files.size() != 0) {
      return IPluginList::STATE_ACTIVE;
    }
  } else {
    log::warn("A dependency on non esp/esm/esl {} will always find it as missing.",
              fileName);
    return IPluginList::STATE_MISSING;
  }

  // If they are really desparate we look in the full mod list and try that
  if (checkDisabledMods()) {
    IModList* modList = m_MOInfo->modList();
    QStringList list  = modList->allMods();
    for (QString mod : list) {
      // Get mod state. if it's active we've already looked. If it's not valid,
      // no point in looking.
      IModList::ModStates state = modList->state(mod);
      if ((state & IModList::STATE_ACTIVE) != 0 ||
          (state & IModList::STATE_VALID) == 0) {
        continue;
      }
      MOBase::IModInterface* modInfo = m_MOInfo->modList()->getMod(mod);
      // Go see if the file is in the mod
      QDir modpath(modInfo->absolutePath());
      QFile file(modpath.absoluteFilePath(fileName));
      if (file.exists()) {
        return IPluginList::STATE_INACTIVE;
      }
    }
  }
  return IPluginList::STATE_MISSING;
}

QList<FomodLogger::PatchEvidence>
InstallerFomod::collectPatchEvidence(const FomodInstallerDialog& dialog,
                                     const std::shared_ptr<IFileTree>& tree)
{
  // Reading masters means extracting the skipped plugins a second time, which is
  // slow for big solid archives. Beyond these limits record plugin names and
  // detection rules only, with masters marked as not read.
  constexpr std::size_t kMaxExtractedPlugins = 400;
  // Silent extraction blocks MO2's UI thread, so keep the worst case short.
  constexpr qint64 kMaxArchiveBytes          = 128LL * 1024 * 1024;

  QList<FomodLogger::PatchEvidence> evidence;
  try {
    auto sources = dialog.patchSources(tree);
    // Only skipped options and automatic installs that did not apply need masters.
    std::vector<std::shared_ptr<const FileTreeEntry>> entries;
    for (const auto& source : sources) {
      if (source.installed) {
        continue;
      }
      for (const auto& entry : source.pluginEntries) {
        if (std::find(entries.begin(), entries.end(), entry) == entries.end()) {
          entries.push_back(entry);
        }
      }
    }

    // Only plugins whose header was actually read have an entry here.
    QHash<const FileTreeEntry*, QStringList> masters;
    const qint64 archiveBytes = QFileInfo(m_Archive).size();
    if (!entries.empty() && entries.size() <= kMaxExtractedPlugins &&
        archiveBytes > 0 && archiveBytes <= kMaxArchiveBytes) {
      // Silent: no progress dialog and nothing the user can cancel. A failed or
      // throwing extraction only leaves the masters unread; plugin names and
      // detection rules are still recorded and the install is unaffected.
      try {
        const QStringList paths = manager()->extractFiles(entries, true);
        if (paths.size() == static_cast<qsizetype>(entries.size())) {
          for (std::size_t i = 0; i < entries.size(); ++i) {
            if (const auto read =
                    FomodLogger::readPluginMasters(paths[static_cast<qsizetype>(i)])) {
              masters.insert(entries[i].get(), *read);
            }
          }
        }
      } catch (const std::exception& e) {
        log::warn("Modlist Companion could not read plugin masters: {}", e.what());
        masters.clear();
      }
    } else if (!entries.empty()) {
      log::debug("Modlist Companion skipped reading masters of {} plugin(s) from a {} byte "
                 "archive",
                 entries.size(), archiveBytes);
    }

    for (FomodInstallerDialog::PatchSources& source : sources) {
      FomodLogger::PatchEvidence item;
      item.id          = source.id;
      item.installed   = source.installed;
      item.plugins     = source.pluginNames;
      item.detects     = source.detects;
      item.files       = std::move(source.files);
      item.hasFiles    = true;
      // Nothing to read means nothing was read: omit masters for such options.
      item.mastersRead = !source.installed && !source.pluginEntries.empty() &&
                         std::all_of(source.pluginEntries.begin(),
                                     source.pluginEntries.end(), [&](const auto& entry) {
                                       return masters.contains(entry.get());
                                     });
      for (const auto& entry : source.pluginEntries) {
        for (const QString& master : masters.value(entry.get())) {
          if (!item.masters.contains(master, Qt::CaseInsensitive) &&
              !item.plugins.contains(master, Qt::CaseInsensitive)) {
            item.masters.append(master);
          }
        }
      }
      evidence.append(item);
    }
    FomodLogger::capFileRecords(evidence);
  } catch (const std::exception& e) {
    // Evidence is a bonus; never let it block an installation.
    log::warn("Modlist Companion could not read patch details: {}", e.what());
    evidence.clear();
  }
  return evidence;
}

IPluginInstaller::EInstallResult
InstallerFomod::install(GuessedValue<QString>& modName,
                        std::shared_ptr<IFileTree>& tree, QString& version, int& modID)
{
  auto installerFiles = buildFomodTree(tree);
  if (manager()->extractFiles(installerFiles).size() == installerFiles.size()) {
    try {
      std::shared_ptr<const IFileTree> fomodTree = findFomodDirectory(tree);

      QString fomodPath = fomodTree->parent()->path();
      FomodInstallerDialog dialog(
          this, modName, fomodPath,
          std::bind(&InstallerFomod::fileState, this, std::placeholders::_1));
      dialog.initData(m_MOInfo);
      if (!dialog.getVersion().isEmpty()) {
        version = dialog.getVersion();
      }
      if (dialog.getModID() != -1) {
        modID = dialog.getModID();
      }

      m_InstallerUsed = true;
      m_Url           = dialog.getURL();

      if (dialog.hasOptions()) {
        offerPreviousChoices(dialog, modName, modID);
      }

      if (!dialog.hasOptions()) {
        dialog.transformToSmallInstall();
      }

      auto result = dialog.exec();
      if (result == QDialog::Accepted) {
        modName.update(dialog.getName(), GUESS_USER);
        // Read patch evidence while the archive tree is still intact; updateTree()
        // replaces it with the install layout.
        const QList<FomodLogger::PatchEvidence> evidence = collectPatchEvidence(dialog, tree);
        const auto installResult = dialog.updateTree(tree);
        if (installResult == IPluginInstaller::RESULT_SUCCESS) {
          QJsonObject instance;
          instance.insert("id", FomodLogger::instanceId(m_MOInfo->basePath()));
          instance.insert("label", QFileInfo(m_MOInfo->basePath()).fileName());

          QJsonObject source;
          source.insert("archive", QFileInfo(m_Archive).fileName());
          source.insert("reinstallation", m_Reinstallation);

          QJsonObject mod;
          mod.insert("name", QString(modName));
          mod.insert("version", version);
          mod.insert("nexus_id", modID);

          QJsonObject installer;
          installer.insert("name", name());
          installer.insert("version", QStringLiteral(FOMOD_LOGGER_VERSION));
          installer.insert("upstream_commit",
                           QStringLiteral("fc263f2d923c704b4853c11ed4f8b8cf3920f30d"));

          m_PendingEvent.insert("schema_version", 1);
          m_PendingEvent.insert(
              "event_id", QUuid::createUuid().toString(QUuid::WithoutBraces).toLower());
          m_PendingEvent.insert("started_at", m_StartedAtUtc);
          m_PendingEvent.insert("instance", instance);
          m_PendingEvent.insert("source", source);
          m_PendingEvent.insert("mod", mod);
          m_PendingEvent.insert("installer", installer);
          m_PendingEvent.insert(
              "steps", FomodLogger::attachPatchEvidence(dialog.selectionSnapshot(), evidence));
          const QJsonArray automatic = FomodLogger::automaticInstalls(evidence);
          if (!automatic.isEmpty()) {
            m_PendingEvent.insert("automatic_installs", automatic);
          }
          const QJsonObject required = FomodLogger::requiredFiles(evidence);
          if (!required.isEmpty()) {
            m_PendingEvent.insert("required_files", required);
          }
        }
        return installResult;
      } else {
        if (dialog.manualRequested()) {
          modName.update(dialog.getName(), GUESS_USER);
          return IPluginInstaller::RESULT_MANUALREQUESTED;
        } else if (result == QDialog::Rejected) {
          return IPluginInstaller::RESULT_CANCELED;
        } else {
          return IPluginInstaller::RESULT_FAILED;
        }
      }
    } catch (const std::exception& e) {
      reportError(tr("Installation as fomod failed: %1").arg(e.what()));
      return IPluginInstaller::RESULT_FAILED;
    }
  }
  return IPluginInstaller::RESULT_CANCELED;
}

#if QT_VERSION < QT_VERSION_CHECK(5, 0, 0)
Q_EXPORT_PLUGIN2(installerFomod, InstallerFomod)
#endif

std::vector<unsigned int> InstallerFomod::activeProblems() const
{
  std::vector<unsigned int> result;
  QList<QByteArray> formats = QImageReader::supportedImageFormats();
  if (!formats.contains("jpg")) {
    result.push_back(PROBLEM_IMAGETYPE_UNSUPPORTED);
  }
  QMutexLocker lock(&m_WriteErrorsMutex);
  if (!m_WriteErrors.isEmpty()) {
    result.push_back(PROBLEM_EVENT_WRITE_FAILED);
  }
  return result;
}

QString InstallerFomod::shortDescription(unsigned int key) const
{
  switch (key) {
  case PROBLEM_IMAGETYPE_UNSUPPORTED:
    return tr("image formats not supported.");
  case PROBLEM_EVENT_WRITE_FAILED:
    return tr("installation choices could not be saved.");
  default:
    throw Exception(tr("invalid problem key %1").arg(key));
  }
}

QString InstallerFomod::fullDescription(unsigned int key) const
{
  switch (key) {
  case PROBLEM_IMAGETYPE_UNSUPPORTED:
    return tr("This indicates that files from dlls/imageformats are missing from your "
              "MO installation or outdated. "
              "Images in installers may not be displayed. Please re-install MO");
  case PROBLEM_EVENT_WRITE_FAILED: {
    QMutexLocker lock(&m_WriteErrorsMutex);
    return tr("Modlist Companion could not save the installer choices of %1 mod(s) in "
              "this MO2 session, so they will not appear in the viewer. The mods "
              "themselves installed normally; reinstalling them records the choices "
              "again once the problem is fixed.\n\n%2")
        .arg(m_WriteErrors.size())
        .arg(m_WriteErrors.join(QStringLiteral("\n")));
  }
  default:
    throw Exception(tr("invalid problem key %1").arg(key));
  }
}

bool InstallerFomod::hasGuidedFix(unsigned int) const
{
  return false;
}

void InstallerFomod::startGuidedFix(unsigned int) const {}
