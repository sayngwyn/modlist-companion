#ifndef FOMODLOGGERVERSION_H
#define FOMODLOGGERVERSION_H

// CMake supplies this definition for release builds. The fallback keeps the
// preserved qmake/SCons developer entry points source-compatible.
#ifndef FOMOD_LOGGER_VERSION
#define FOMOD_LOGGER_VERSION "1.0.0"
#endif

// Keep the MO2 identity explicit, even when it matches the public version.
#ifndef FOMOD_LOGGER_MO2_VERSION
#define FOMOD_LOGGER_MO2_VERSION "1.0.0"
#endif

#endif  // FOMODLOGGERVERSION_H
