#ifndef FOMODLOGGEREVENTWRITER_H
#define FOMODLOGGEREVENTWRITER_H

#include <QJsonObject>
#include <QString>

namespace FomodLogger
{

struct EventWriteResult
{
  bool success = false;
  QString path;
  QString error;
};

QString instanceId(const QString& basePath);

/**
 * @brief Write the event to <pluginDataPath>/fomod_logger/events/<event-id>.json.
 *
 * The file appears complete or not at all (QSaveFile writes a temporary file and
 * renames it into place). An existing event file is never replaced.
 */
EventWriteResult writeEvent(const QString& pluginDataPath, const QJsonObject& event);

}  // namespace FomodLogger

#endif  // FOMODLOGGEREVENTWRITER_H
