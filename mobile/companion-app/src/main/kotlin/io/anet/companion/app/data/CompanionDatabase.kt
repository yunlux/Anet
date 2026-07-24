package io.anet.companion.app.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

@Database(
    entities = [
        ConsentGrantEntity::class,
        OutboxEntity::class,
        InterventionEntity::class,
    ],
    version = 1,
    exportSchema = true,
)
abstract class CompanionDatabase : RoomDatabase() {
    abstract fun companionDao(): CompanionDao

    companion object {
        fun open(context: Context): CompanionDatabase =
            Room.databaseBuilder(
                context.applicationContext,
                CompanionDatabase::class.java,
                "companion-state.sqlite3",
            )
                .build()
    }
}
