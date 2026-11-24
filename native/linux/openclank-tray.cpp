#include <QApplication>
#include <QDBusConnection>
#include <QDesktopServices>
#include <QFileInfo>
#include <QMenu>
#include <QProcess>
#include <QSystemTrayIcon>
#include <QUrl>

int main(int argc, char **argv)
{
    QApplication app(argc, argv);
    app.setApplicationName("Open Clank");
    app.setQuitOnLastWindowClosed(false);
    if (!QDBusConnection::sessionBus().registerService("org.openclank.Tray"))
        return 1;

    const QString icon = app.applicationDirPath() + "/openclank.png";
    if (!QFileInfo::exists(icon))
        return 1;
    QSystemTrayIcon tray{QIcon(icon)};
    tray.setToolTip("Open Clank — http://127.0.0.1:7777");
    QMenu menu;
    const auto open = [&] {
        if (!QDesktopServices::openUrl(QUrl("http://127.0.0.1:7777")))
            tray.showMessage("Open Clank", "Could not open the local app in your browser.");
    };
    menu.addAction("Open Clank", open);
    menu.addSeparator();
    QAction *start = menu.addAction("Start server");
    QAction *stop = menu.addAction("Stop server");
    menu.addSeparator();
    QAction *trayOnly = menu.addAction("Quit tray only (server keeps running)");
    QAction *quit = menu.addAction("Quit Open Clank");

    QProcess service;
    bool quitAfterStop = false;
    const auto setBusy = [&](bool busy) {
        for (auto *action : {start, stop, trayOnly, quit})
            action->setEnabled(!busy);
    };
    const auto control = [&](const QString &command, bool exitOnSuccess) {
        if (service.state() != QProcess::NotRunning)
            return;
        quitAfterStop = exitOnSuccess;
        setBusy(true);
        service.start("/usr/bin/systemctl", {"--user", command, "openclank.service"});
    };
    const auto failed = [&] {
        quitAfterStop = false;
        setBusy(false);
        tray.showMessage("Open Clank", "Server control failed. Check openclank.service logs.");
    };
    QObject::connect(&service, &QProcess::errorOccurred, [&](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart)
            failed();
    });
    QObject::connect(&service, &QProcess::finished, [&](int code, QProcess::ExitStatus status) {
        if (code != 0 || status != QProcess::NormalExit)
            failed();
        else if (quitAfterStop)
            app.quit();
        else
            setBusy(false);
    });
    QObject::connect(start, &QAction::triggered, [&] { control("start", false); });
    QObject::connect(stop, &QAction::triggered, [&] { control("stop", false); });
    QObject::connect(quit, &QAction::triggered, [&] { control("stop", true); });
    QObject::connect(trayOnly, &QAction::triggered, &app, &QApplication::quit);
    QObject::connect(&tray, &QSystemTrayIcon::activated, [&](auto reason) {
        if (reason == QSystemTrayIcon::Trigger)
            open();
    });
    tray.setContextMenu(&menu);
    tray.show();
    return app.exec();
}
