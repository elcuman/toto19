from app import app, sync_champions_league

if __name__ == "__main__":
    with app.app_context():
        result = sync_champions_league()
        print(result)