# DreamPlanner
Aplikacja do planowania dyspozycyjności oraz elastycznego zarządzania grafikami zleceniobiorców oraz pracowników.

## Stos technologiczny 
- **Frontend:** React, Vite, TypeScript
- **Backend:** Python, FastAPI, Pydantic, SQLAlchemy, Alembic
- **Baza danych:** PostgreSQL | Azure Database for PostgreSQL - Flexible Server
- **Chmura & DevOps:** Microsoft Azure

## Architektura systemu

System oparty jest na klasycznej, trójwarstwowej architekturze modularnej z separacją warstwy prezentacji, aplikacji oraz danych.

- **Warstwa prezentacji** odpowiada za interfejs użytkownika i komunikację z backendem za pośrednictwem REST API.
- **Warstwa aplikacji** realizuje logikę biznesową, obsługuje uwierzytelnianie i autoryzację oraz koordynuje operacje na danych. Backend zostanie zaimplementowany w Pythonie z wykorzystaniem frameworka FastAPI.
- **Warstwa danych** odpowiada za przechowywanie informacji w relacyjnej bazie danych PostgreSQL. SQLAlchemy zapewni warstwę dostępu do danych, a Alembic umożliwi zarządzanie migracjami schematu bazy danych.

Komunikacja pomiędzy frontendem a backendem będzie realizowana za pośrednictwem REST API z wykorzystaniem protokołu HTTPS oraz formatu JSON do przesyłania danych.

```mermaid
flowchart TD
    U["Użytkownik / Przeglądarka"]

    subgraph Presentation["Warstwa prezentacji"]
        FE["Frontend Web App<br/>React / Vite / TypeScript"]
    end

    subgraph Application["Warstwa aplikacji"]
        API["REST API<br/>FastAPI"]
        AUTH["Uwierzytelnianie i autoryzacja"]
        BL["Logika biznesowa"]
        ORM["Warstwa dostępu do danych<br/>SQLAlchemy"]
    end

    subgraph Data["Warstwa danych"]
        DB[("PostgreSQL<br/>Azure Flexible Server")]
        MIG["Migracje schematu<br/>Alembic"]
    end

    U --> FE
    FE -->|"HTTPS / JSON"| API
    API --> AUTH
    API --> BL
    BL --> ORM
    ORM -->|"SQL"| DB
    MIG -.->|"Zmiany schematu"| DB