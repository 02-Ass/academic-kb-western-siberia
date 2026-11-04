import { useState } from "react";
import Icon from "./Icon";

export default function SearchBar({ onSearch }) {
  const [value, setValue] = useState("");

  function handleSubmit(e) {
    e.preventDefault();
    if (value.trim()) onSearch(value.trim());
  }

  return (
    <form id="search-form" onSubmit={handleSubmit}>
      <Icon name="search" size={20} className="search-ico" />
      <input
        id="search-input"
        type="text"
        placeholder="Например: гидрогеологические исследования Шаимского района"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <button type="submit">Найти</button>
    </form>
  );
}
