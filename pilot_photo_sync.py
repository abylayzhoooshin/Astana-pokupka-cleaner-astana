"""One explicit local pull of purchase Main photo results for the bounded pilot."""

import cleaner_db
import main_photo_sync


def main():
    with cleaner_db.connect() as conn:
        result = main_photo_sync.sync(conn)
    print(result)


if __name__ == "__main__":
    main()
