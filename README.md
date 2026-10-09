# DreamPlanner
Aplikacja do planowania dyspozycyjności oraz elastycznego zarządzania grafikami zleceniobiorców oraz pracowników.

## Stos technologiczny 
- **Frontend:** React, Vite, TypeScript
- **Backend:** REST API Backend 
- **Baza danych:** PostgreSQL | Azure Database for PostgreSQL - Flexible Server
- **Chmura & DevOPS:** Microsoft Azure

## Architektura systemu

System oparty jest na klasycznej, trójwarstwowej architekturze modularnej z separacją warstwy prezentacji, aplikacji oraz danych:

```mermaid
flowchart TD
    U[Użytkownik / Przeglądarka]

    subgraph Presentation Layer [Warstwa Prezentacji]
        FE[Frontend Web App - React / Vite]
    end

    subgraph Application Layer [Warstwa Aplikacji]
        API[REST API Backend]
        AUTH[Authentication Service]
    end

    subgraph Data Layer [Warstwa Danych]
        DB[(PostgreSQL Database - Azure Flexible Server)]
    end

    U --> FE
    FE --> API
    API --> AUTH
    API --> DB
