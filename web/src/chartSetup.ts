import {
  Chart as ChartJS,
  LineElement,
  PointElement,
  LinearScale,
  TimeScale,
  Tooltip,
  Filler,
} from "chart.js";
import "chartjs-adapter-luxon";

ChartJS.register(LineElement, PointElement, LinearScale, TimeScale, Tooltip, Filler);
