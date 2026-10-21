"""Przykładowe dane (format tabeli Timocom, kolumny rozdzielone tabulacją) do szybkiego wypróbowania aplikacji."""

SAMPLE_TEXT = "\n".join("\t".join(r) for r in [
    ("FR 77140 Nemours", "GB G1 1 Glasgow", "07.10", "22,00", "13,60", "1176 km", "2500,00 EUR"),
    ("FR 91300 Massy", "GB LS25 2 Leeds (Garforth)", "07.10", "20,00", "13,60", "772 km", "2300,00 EUR"),
    ("FR 77100 Meaux", "GB NN11 4 Daventry", "07.10", "0,05", "0,50", "583 km", "600,00 EUR"),
    ("FR 95000 Cergy", "GB NN13 7 Brackley", "07.10", "5,00", "3,50", "564 km", "900,00 EUR"),
    ("FR 27000 Évreux", "GB NN1 1 Northampton", "07.10", "6,00", "4,00", "552 km", "950,00 EUR"),
    ("FR 60310 Lassigny", "GB WR1 2 Worcester", "07.10", "24,00", "13,60", "600 km", "2200,00 EUR"),
    ("FR 93200 Saint-Denis", "GB B3 3 Birmingham", "08.10", "2,00", "6,70", "645 km", "-"),
]) + "\n"
