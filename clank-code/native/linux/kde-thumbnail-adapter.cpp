// Optional host-desktop adapter. Its only successful render result is content
// emitted by KIO PreviewJob; no file-icon fallback is used.
#include <QGuiApplication>
#include <QBuffer>
#include <QFileInfo>
#include <QPixmap>
#include <QTimer>
#include <QTemporaryDir>
#include <KConfigGroup>
#include <KSharedConfig>
#include <QUrl>
#include <KFileItem>
#include <KIO/PreviewJob>
#include <cstdio>
#include <sys/prctl.h>
#include <signal.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    // The broker owns our process group. Also die if the immediate helper dies
    // unexpectedly, even before its broker can reap the group.
    const auto parent = getppid();
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) || getppid() != parent) return 1;
    if (qEnvironmentVariableIsEmpty("QT_QPA_PLATFORM")) qputenv("QT_QPA_PLATFORM", "offscreen");
    // A rejected shared-cache entry must not be reaccepted by KIO's
    // seconds-only cache reader. Create its private cache before Qt initializes.
    QTemporaryDir freshCache;
    const bool fresh = argc == 6 && QByteArray(argv[5]) == "--fresh";
    if (fresh) {
        if (!freshCache.isValid()) return 1;
        qputenv("XDG_CACHE_HOME", freshCache.path().toLocal8Bit());
    }
    QGuiApplication app(argc, argv);
    if (argc == 2 && QByteArray(argv[1]) == "--probe") {
        const KConfigGroup settings(KSharedConfig::openConfig(), QStringLiteral("PreviewSettings"));
        const auto enabled = settings.readEntry("Plugins", QStringList{QStringLiteral("directorythumbnail"), QStringLiteral("imagethumbnail"), QStringLiteral("jpegthumbnail")});
        const auto available = KIO::PreviewJob::availablePlugins();
        for (const auto &plugin : enabled) if (available.contains(plugin)) return 0;
        return 1;
    }
    if (argc != 5 && !fresh) { std::fprintf(stderr,"thumbnail-adapter invalid arguments\n"); return 1; }
    bool widthOk = false, heightOk = false, limitOk = false;
    const int width = QByteArray(argv[2]).toInt(&widthOk);
    const int height = QByteArray(argv[3]).toInt(&heightOk);
    const int limit = QByteArray(argv[4]).toInt(&limitOk);
    if (!widthOk || !heightOk || !limitOk || width < 1 || height < 1 || width > 3072 || height > 3072 || limit < 1 || limit > 4*1024*1024) return 1;
    const QFileInfo source(QString::fromLocal8Bit(argv[1]));
    if (!source.isAbsolute() || !source.isFile() || !source.isReadable()) { std::fprintf(stderr,"thumbnail-adapter ineligible source\n"); return 1; }
    const KFileItem item(QUrl::fromLocalFile(source.absoluteFilePath()));
    auto *job = KIO::filePreview(KFileItemList{item}, QSize(width, height));
    // Null plugin selection preserves the host's PreviewSettings/defaults.
    job->setScaleType(KIO::PreviewJob::ScaledAndCached);
    int result = 1;
    QObject::connect(job, &KIO::PreviewJob::gotPreview, &app, [&](const KFileItem &, const QPixmap &preview) {
        if (preview.isNull()) return;
        const QImage image = preview.toImage().scaled(width, height, Qt::KeepAspectRatio, Qt::SmoothTransformation);
        QByteArray bytes;
        QBuffer buffer(&bytes);
        if (!buffer.open(QIODevice::WriteOnly) || !image.save(&buffer, "PNG") || bytes.size() > limit) return;
        if (std::fwrite(bytes.constData(), 1, bytes.size(), stdout) == static_cast<size_t>(bytes.size()) && std::fflush(stdout) == 0) result = 0;
    });
    QObject::connect(job, &KIO::PreviewJob::failed, &app, [&](const KFileItem &) { std::fprintf(stderr,"thumbnail-adapter native preview unavailable\n"); });
    QObject::connect(job, &KJob::result, &app, [&] { app.exit(result); });
    // Outer broker normally enforces a shorter request deadline. This ceiling
    // also bounds direct adapter use; never start a nested event loop on error.
    QTimer::singleShot(15000, &app, [&] { job->kill(); app.exit(1); });
    return app.exec();
}
