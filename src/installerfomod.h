#ifndef INSTALLERFOMOD_H
#define INSTALLERFOMOD_H

#include <iplugindiagnose.h>
#include <iplugininstallersimple.h>
#include <ipluginlist.h>

#include <QJsonObject>
#include <QList>
#include <QMutex>
#include <QStringList>

#include "fomodpatchevidence.h"

class FomodInstallerDialog;

class InstallerFomod : public MOBase::IPluginInstallerSimple,
                       public MOBase::IPluginDiagnose
{

  Q_OBJECT
  Q_INTERFACES(MOBase::IPlugin MOBase::IPluginInstaller MOBase::IPluginInstallerSimple
                   MOBase::IPluginDiagnose)
#if QT_VERSION >= QT_VERSION_CHECK(5, 0, 0)
  Q_PLUGIN_METADATA(IID "org.sayngwyn.FomodLogger" FILE "installerfomod.json")
#endif

public:
  InstallerFomod();

  virtual bool init(MOBase::IOrganizer* moInfo) override;
  virtual QString name() const override;
  virtual QString localizedName() const override;
  virtual QString author() const override;
  virtual QString description() const override;
  virtual MOBase::VersionInfo version() const override;
  virtual QList<MOBase::PluginSetting> settings() const override;

  virtual unsigned int priority() const override;
  virtual bool isManualInstaller() const override;

  virtual bool
  isArchiveSupported(std::shared_ptr<const MOBase::IFileTree> tree) const override;
  virtual EInstallResult install(MOBase::GuessedValue<QString>& modName,
                                 std::shared_ptr<MOBase::IFileTree>& tree,
                                 QString& version, int& modID) override;

  virtual void onInstallationStart(QString const& archive, bool reinstallation,
                                   MOBase::IModInterface* currentMod) override;
  virtual void onInstallationEnd(EInstallResult result,
                                 MOBase::IModInterface* newMod) override;

public:  // IPluginDiagnose interface
  virtual std::vector<unsigned int> activeProblems() const;
  virtual QString shortDescription(unsigned int key) const;
  virtual QString fullDescription(unsigned int key) const;
  virtual bool hasGuidedFix(unsigned int key) const;
  virtual void startGuidedFix(unsigned int key) const;

private:
  /**
   * @brief Retrieve the tree entry corresponding to the fomod directory.
   *
   * @param tree Tree to look-up the directory in.
   *
   * @return the entry corresponding to the fomod directory in the tree, or a null
   * pointer if the entry was not found.
   */
  std::shared_ptr<const MOBase::IFileTree>
  findFomodDirectory(std::shared_ptr<const MOBase::IFileTree> tree) const;

  /**
   * @brief Build a list of entries that should be extracted sincce the FOMOD installer
   * may require access to (currently the .xml files in the FOMOD directory and the
   * pictures in the archive).
   *
   * @param tree Base tree of the archive.
   *
   * @return a list of file entries that need to be extracted.
   */
  std::vector<std::shared_ptr<const MOBase::FileTreeEntry>>
  buildFomodTree(std::shared_ptr<const MOBase::IFileTree> tree) const;

  /**
   * @brief Recurse through the given tree and add all the images to the given vector.
   *
   * @param result Vector of entries to add the images.
   * @param tree The tree to look files in.
   */
  void
  appendImageFiles(std::vector<std::shared_ptr<const MOBase::FileTreeEntry>>& entries,
                   std::shared_ptr<const MOBase::IFileTree> tree) const;

  /**
   * @brief Record which plugins each option would install, their masters and the
   * FOMOD's own plugin detection rules. Must run before the dialog rewrites the tree.
   */
  QList<FomodLogger::PatchEvidence>
  collectPatchEvidence(const FomodInstallerDialog& dialog,
                       const std::shared_ptr<MOBase::IFileTree>& tree);

  MOBase::IPluginList::PluginStates fileState(const QString& fileName) const;

  /**
   * @brief Show this mod's last recorded choices in the dialog, if there are any
   * that line up with it; failing that, the choices of an imported choices file
   * (offered, never applied unasked). Never blocks or fails an installation.
   */
  void offerPreviousChoices(FomodInstallerDialog& dialog,
                            const MOBase::GuessedValue<QString>& modName,
                            int nexusId) const;

private:
  static const unsigned int PROBLEM_IMAGETYPE_UNSUPPORTED = 1;
  static const unsigned int PROBLEM_EVENT_WRITE_FAILED     = 2;

private:
  MOBase::IOrganizer* m_MOInfo;

  bool allowAnyFile() const;
  bool checkDisabledMods() const;
  bool restorePreviousChoices() const;

  bool m_InstallerUsed;
  QString m_Url;
  QString m_Archive;
  QString m_StartedAtUtc;
  QStringList m_WriteErrors;  // events that could not be saved this session
  mutable QMutex m_WriteErrorsMutex;
  bool m_Reinstallation = false;
  QString m_ReinstalledModName;  // the mod being reinstalled, if any
  QJsonObject m_PendingEvent;
};

#endif  // INSTALLERFOMOD_H
