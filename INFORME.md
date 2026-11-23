# CJAR — Vulnerabilidades y fortalezas conocidas

Informe breve que acompaña a la entrega (noviembre de 2026). Todo lo que sigue se ha **medido ejecutando el sistema**, salvo donde se dice lo contrario.

**Sistema:** detector SCRFD-500M y extractor ArcFace MobileFaceNet (InsightFace `buffalo_s`), similitud coseno con umbral **0,48** y margen **0,05**, BD en CSV cifrada y sellada. **50 personas**: las 49 fotos que trae `Dataset.tar.gz` (el enunciado dice 50) y `salman_rushdie`.

## 1. Fortalezas

| Supuesto o ataque | Qué pasa |
|---|---|
| **El atacante lee la BD** | Cada plantilla va cifrada (AES-256-GCM). La celda robada no sirve en `verificar_t` ni en `identificar_t` |
| **Plantilla calculada con otro ArcFace o filtrada antes** | 0 de 50 aceptadas: el extractor aplica una rotación secreta (plantilla cancelable); cambiando la clave las anteriores quedan inservibles |
| **El atacante escribe en la BD** | 14 ediciones probadas (añadir, sustituir, intercambiar, duplicar, renombrar, reordenar, borrar o truncar filas, quitar el sello, resellar con otra clave, cambiar un bit, vaciar, borrar el fichero, poner una BD en claro): las 14 se rechazan y el sistema **falla cerrado** con aviso en el log |
| **Se salta el detector (`_sd`)** | Imágenes lisas, ruido y 1×1 se rechazan por calidad. Un array float 0-1 o uint16 se escala bien (recortarlo haría iguales todas las caras) |
| **Se salta detector y extractor (`_t`)** | NaN, infinitos, ceros, dimensiones o tipos erróneos se rechazan. Una plantilla «lobo» (media de varias personas) no verifica como más de una |
| **Ruido adversario invisible** (atacante con los gradientes que **no** conoce la defensa) | 0 de 10 víctimas aceptadas con ±4, ±8 y ±16/255, en `_sd` y con foto completa (sin la defensa: 10/10 en `_sd` desde ±4 y 7/10 y 10/10 con foto completa a ±8 y ±16) |
| **Quitarle la identidad a otro** | Reinscribir un id no lo sustituye; no se inscribe a quien ya está con otro id, ni un id que se confunda (`Adam_Pearson`, letras cirílicas, `a1ice`), ni una lista de fotos de personas distintas |
| **Errores de identificación** | LFW: 653.250 comparaciones de impostores, 1 por encima de 0,48 (un gemelo) y **0 aceptadas** con el sistema completo; 0 de 13.065 fotos de impostores identificadas. Las 49 fotos del dataset se verifican e identifican (49/49) |
| **Web** | Sin puntuaciones ni ranking; clave de administración para inscribir, borrar y listar; CSRF, DNS rebinding, CSP, límites de tamaño y cola; imagen sin root, de solo lectura, sin capacidades; dependencias con SHA-256 y sin vulnerabilidades conocidas en OSV (23/11/2026) |

La suite (`tests/`, 114 pruebas) cubre estas defensas.

## 2. Vulnerabilidades y limitaciones que conocemos

### Inherentes o no resueltas

1. **No hay detección de vivacidad.** Una foto (o una pantalla) de la víctima verifica como ella: el sistema recibe fotos, no capturas en directo.
2. **Gemelos y dobles.** La cara sola no los separa: una foto de Oliver Phelps da 0,505 con James (inscrito). Hay *contrastes* (`registrar_contraste`) para los gemelos conocidos y la BD trae dos (James Phelps y Mary-Kate Olsen). A cambio también rechazan fotos auténticas: de Mary-Kate se aceptaban 2 de 3 de LFW y ahora 0. Un gemelo sin contraste (el de Twin Melody, por ejemplo) puede colarse.
3. **Envejecimiento y cambios de aspecto: rechaza a personas auténticas.** Con una foto por persona y un umbral alto, en LFW se acepta a 31 de 121 fotos de inscritos: Keanu Reeves 11/12, Salma Hayek 9/13, Jackie Chan 7/13, pero Serena Williams 0/51, Isabel II 0/13 y Michael Jackson 0/12. En parejas de LFW en general el falso rechazo es del 12 % (4 % con umbral 0,40). Es una elección: preferimos rechazar a suplantar.
4. **Ataque adversario que conoce la defensa.** Optimizando también contra el suavizado, con la clave y los gradientes: en `_sd`, 0/10 con ±4/255, 7/10 con ±8 y 10/10 con ±16; con foto completa, 0/10, 1/10 y 10/10. Desde ruido puro, 4/10.
5. **Morphing de consulta.** Mezclando píxel a píxel una cara ajena y la de la víctima, `verificar_sd` acepta 26 de 60 al 50 %, 8 de 60 con un 40 % de víctima y 0 de 60 con un 30 %. No hay detector de morphing.
6. **Quien tiene la clave lo tiene todo.** `clave/bd.key` va en el repositorio para que funcione al clonarlo; con ella (o ejecutando código en el proceso, p. ej. `s.db.get_template`) se leen y se escriben plantillas. El cifrado protege frente a quien solo accede al CSV. Además, con las fotos del docente y nuestro extractor se recalcula cualquier plantilla. El historial de git conserva la BD anterior en claro: no sirve en este sistema sin la clave.
7. **Copia antigua de la BD.** Restaurar un CSV anterior legítimo se detecta con el sistema en marcha, pero no si se hace con él parado.
8. **Escribir en el CSV es una denegación de servicio** (falla cerrado). Se recupera con `git checkout database/database.csv`. Los ids (nombres) se ven en claro.
9. **La librería confía en quien la llama:** `reemplazar=True` y `permitir_parecido=True` desactivan las reglas de inscripción. Con un clon permitido, la víctima deja de verificarse (empate).

### Robustez y calidad

10. **Inscripción de mala calidad.** Se exige una cara de 64 px o más y calidad mínima, pero en LFW pasan caras muy desenfocadas (15 de 40), con los ojos tapados (35 de 40) o media cara (21 de 40), y después no se reconoce a la persona en 15/15, 26/35 y 11/21.
11. **Doble inscripción con otra foto.** Si las dos fotos se parecen menos de 0,40 la deduplicación no lo ve (2 de 36 en LFW).
12. **Orientación.** Con la foto girada 90° acierta 16 de 25, y a 180° ninguna; no se reintenta girada. Varias caras: se usa la mayor (al inscribir se rechaza si la segunda mide la mitad o más).
13. **`_sd` exige una cara alineada de 112×112.** Con la foto entera da `False` o una plantilla inservible.
14. **Uso raro de los datos:** los arrays se interpretan en BGR (en RGB baja la similitud, 0,89 consigo mismo) y no se admiten imágenes animadas ni con transparencia.
15. **Sesgo.** No se ha medido por grupos demográficos (50 personas no bastan). El fabricante no publica el desglose de este modelo que hayamos podido comprobar.
16. **Disponibilidad.** Con más de 1024 conexiones lentas la web responde 503 (solo lo resuelve un proxy inverso); escucha solo en localhost. La clave de administración no limita intentos y, si no se define, sale en el log.
17. **Sin Docker en Windows** no hay bloqueo entre procesos (no existe `fcntl`): dos procesos escribiendo a la vez pueden perder una inscripción.
18. **Licencia:** los modelos de InsightFace son solo para investigación no comercial.

## 3. Cómo reproducirlo

`docker build --build-arg INSTALL_DEV=1 -t cjar:test . && docker run --rm --network none cjar:test python -m unittest tests.test_system tests.test_bd tests.test_imagen tests.test_web tests.test_avisos tests.test_despliegue`. LFW es el conjunto `lfw_funneled` (13.233 fotos; en 47 no se detectó cara).
