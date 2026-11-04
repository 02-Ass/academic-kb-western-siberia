import { MapContainer, TileLayer, CircleMarker, Tooltip } from "react-leaflet";
import "leaflet/dist/leaflet.css";

// Координаты есть только у месторождений (field) — раздел 3.4
// лабораторной работы, единственная сущность с полем coordinates.
export default function MapView({ features, onSelectField }) {
  return (
    <MapContainer center={[60.5, 65.0]} zoom={6} style={{ height: "100%", width: "100%" }}>
      <TileLayer
        attribution='&copy; OpenStreetMap contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      {features.map((f) => {
        const [lon, lat] = f.geometry.coordinates;
        return (
          <CircleMarker
            key={f.properties.id}
            center={[lat, lon]}
            radius={8}
            pathOptions={{ color: "#0a1628", weight: 1.5, fillColor: "#3b8dd9", fillOpacity: 0.92 }}
            eventHandlers={{ click: () => onSelectField(f.properties.id) }}
          >
            <Tooltip>{f.properties.name}</Tooltip>
          </CircleMarker>
        );
      })}
    </MapContainer>
  );
}
