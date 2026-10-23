#include "fomodloggereventwriter.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFileInfo>
#include <QJsonDocument>
#include <QSaveFile>
#include <QUuid>

namespace FomodLogger
{

QString instanceId(const QString& basePath)
{
  const QByteArray normalized = QDir::cleanPath(basePath).toCaseFolded().toUtf8();
  return QString::fromLatin1(
      QCryptographicHash::hash(normalized, QCryptographicHash::Sha256).toHex());
}

EventWriteResult writeEvent(const QString& pluginDataPath, const QJsonObject& event)
{
  EventWriteResult result;

  const QString eventId = event.value("event_id").toString();
  const QUuid eventUuid(eventId);
  if (eventUuid.isNull() ||
      eventUuid.toString(QUuid::WithoutBraces).toLower() != eventId) {
    result.error = QStringLiteral("installation event has an invalid event_id");
    return result;
  }

  const QString directory =
      QDir(pluginDataPath).filePath(QStringLiteral("fomod_logger/events"));
  if (!QDir().mkpath(directory)) {
    result.error = QStringLiteral("could not create folder %1").arg(directory);
    return result;
  }

  // Event IDs are fresh UUIDs, so an existing file means something is badly wrong;
  // refuse rather than overwrite recorded history.
  const QString path = QDir(directory).filePath(eventId + QStringLiteral(".json"));
  if (QFileInfo::exists(path)) {
    result.error = QStringLiteral("%1 already exists").arg(path);
    return result;
  }

  QSaveFile file(path);
  file.setDirectWriteFallback(false);
  const QByteArray payload = QJsonDocument(event).toJson(QJsonDocument::Indented);
  if (!file.open(QIODevice::WriteOnly) || file.write(payload) != payload.size() ||
      !file.commit()) {
    result.error = QStringLiteral("could not write %1: %2").arg(path, file.errorString());
    file.cancelWriting();
    return result;
  }

  result.success = true;
  result.path    = path;
  return result;
}

}  // namespace FomodLogger
