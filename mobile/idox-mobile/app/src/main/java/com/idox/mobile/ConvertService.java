package com.idox.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

/**
 * Keeps the app in the foreground while a conversion runs, with a notification.
 *
 * Without it, Android's low-memory killer stopped the app (and the model server with it)
 * as soon as the user left the app during a 2B run: a background app holding ~2.5 GB is
 * the first thing it reclaims. A foreground service tells Android the work is wanted.
 * The conversion itself still runs in MainActivity; this only holds the priority.
 */
public class ConvertService extends Service {

    private static final String CHANNEL = "convert";
    private static final int ID = 1;

    static void start(Context ctx, String text) {
        Intent i = new Intent(ctx, ConvertService.class).putExtra("text", text);
        if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
        else ctx.startService(i);
    }

    static void stop(Context ctx) {
        ctx.stopService(new Intent(ctx, ConvertService.class));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String text = intent == null ? "Converting..." : intent.getStringExtra("text");
        NotificationManager nm = getSystemService(NotificationManager.class);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Conversions",
                    NotificationManager.IMPORTANCE_LOW));
            b = new Notification.Builder(this, CHANNEL);
        } else {
            b = new Notification.Builder(this);
        }
        Notification n = b.setContentTitle("idox mobile")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(ID, n);
        }
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
