# 🛰️ GPS Tracker — Sistema di Monitoraggio Percorsi

Container con web app Flask + server FTP integrati.

## Struttura
```
gps-tracker/
├── app/                  # Applicazione Flask
│   ├── app.py
│   ├── templates/
│   └── requirements.txt
├── Dockerfile            # Container unico (Flask + vsftpd)
├── docker-compose.yml
├── supervisord.conf      # Gestione processi interni
├── vsftpd.conf           # Config FTP
├── entrypoint.sh
├── .env.example
└── README.md
```

## Installazione

```bash
# 1. Copia i file sul server
cd /opt/gps-tracker

# 2. Configura
cp .env.example .env
nano .env

# 3. Avvia
docker compose up -d
```

## Accesso
- **Web app:** http://IP_SERVER:5000
- **FTP:** ftpuser@IP_SERVER (porta 21)
  - Cartella upload: `/uploads`
  - Formati accettati: `.gpx` `.csv` `.json`

## File .env
```
APP_PASSWORD=tuapassword
SECRET_KEY=chiave-segreta-lunga
FTP_USER=ftpuser
FTP_PASS=ftppassword
SERVER_IP=IP_DEL_SERVER
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=tua@gmail.com
SMTP_PASS=app_password_gmail
REPORT_EMAIL=destinatario@email.com
```

## Comandi utili
```bash
# Logs
docker compose logs -f

# Riavvio
docker compose restart

# Aggiornamento dopo modifica .env
docker compose down && docker compose up -d
```
